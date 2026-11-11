"""Size of a UADP NetworkMessage and of the Ethernet frame that carries it.

NetworkMessage layout: "UADP-Periodic-Fixed", OPC 10000-14 Annex A.2.1
(header layout URI http://opcfoundation.org/UA/PubSub-Layouts/UADP-Periodic-Fixed).

NetworkMessage header (Figure A.1 / Table A.1)
    Version/Flags                1
    ExtendedFlags1               1
    PublisherId                  2 (UInt16) or 8 (UInt64)
    GroupHeader: GroupFlags      1
                 WriterGroupId   2
                 GroupVersion    4
                 NetworkMessageNumber 2
                 SequenceNumber  2
    PayloadHeader                disabled (no Count, DataSetWriterIds or Sizes)
    SecurityHeader, Timestamp, PicoSeconds, ExtendedFlags2: disabled
    -> 13 + PublisherId bytes

DataSetMessage header (Figure A.4 / Tables A.5, A.6)
    DataSetFlags1                1
    DataSetMessageSequenceNumber 2
    Status                       2
    -> 5 bytes per DataSetMessage

DataSet fields: RawData encoding (7.2.4.5.11), i.e. OPC 10000-6 binary
encoding without type information; String/ByteString padded to
MaxStringLength, arrays padded to ArrayDimensions.

Transport (A3: UDP unicast over IPv4, A5: one frame, no fragmentation)
    IPv4 header 20, UDP header 8; IP packet <= 1500 bytes (MTU)
    Ethernet header 14, IEEE 802.1Q tag 4 (carries the PCP), FCS 4,
    minimum frame size 64 bytes
    On the wire additionally: preamble + SFD 8, inter-frame gap 12
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# OPC 10000-6 binary encoding sizes of the fixed-size built-in types.
FIXED_SIZES = {
    "Boolean": 1, "SByte": 1, "Byte": 1,
    "Int16": 2, "UInt16": 2,
    "Int32": 4, "UInt32": 4, "Float": 4, "StatusCode": 4,
    "Int64": 8, "UInt64": 8, "Double": 8, "DateTime": 8,
    "Guid": 16,
}
STRING_TYPES = ("String", "ByteString")      # Int32 length + MaxStringLength bytes
PUBLISHER_ID_BYTES = {"UInt16": 2, "UInt64": 8}

NM_HEADER_FIXED = 1 + 1 + 1 + 2 + 4 + 2 + 2  # all NM header fields except PublisherId
DSM_HEADER = 1 + 2 + 2
IPV4_HEADER, UDP_HEADER = 20, 8
ETH_HEADER, VLAN_TAG, FCS = 14, 4, 4
MIN_FRAME = 64
PREAMBLE_SFD, IFG = 8, 12
MTU = 1500

_TYPE_RE = re.compile(r"^([A-Za-z][A-Za-z0-9]*)(?:\((\d+)\))?(?:\[(\d+)\])?$")


class SizeError(ValueError):
    pass


def field_size(spec: str) -> int:
    """Encoded size of one DataSet field with RawData encoding.

    Accepted forms: 'Double', 'String(32)', 'Float[8]', 'String(16)[4]'.
    (n) is MaxStringLength, [n] is the array length (one dimension).
    """
    m = _TYPE_RE.match(str(spec).strip())
    if not m:
        raise SizeError(f"cannot parse field type {spec!r}")
    name, max_len, dim = m.group(1), m.group(2), m.group(3)
    if name in STRING_TYPES:
        if max_len is None:
            raise SizeError(f"{spec!r}: {name} needs a MaxStringLength, e.g. {name}(32)")
        elem = 4 + int(max_len)
    elif name in FIXED_SIZES:
        if max_len is not None:
            raise SizeError(f"{spec!r}: only String/ByteString take a length in ()")
        elem = FIXED_SIZES[name]
    else:
        raise SizeError(f"{spec!r}: unsupported type; use one of "
                        f"{', '.join(list(FIXED_SIZES) + list(STRING_TYPES))}")
    if dim is None:
        return elem
    return 4 + int(dim) * elem               # Int32 length + padded elements


@dataclass(frozen=True)
class FrameSize:
    nm: int          # UADP NetworkMessage
    ip_packet: int   # IPv4 + UDP + NM
    frame: int       # Ethernet frame incl. header, VLAN tag, FCS, padding to 64
    wire: int        # frame + preamble/SFD + IFG

    def length(self, preamble_ifg: bool) -> int:
        return self.wire if preamble_ifg else self.frame


def nm_size(types: list[str], n_dsm: int, publisher_id_type: str = "UInt16") -> int:
    """Bytes of one UADP-Periodic-Fixed NetworkMessage."""
    if publisher_id_type not in PUBLISHER_ID_BYTES:
        raise SizeError(f"publisher_id_type must be UInt16 or UInt64 (Table A.1), got {publisher_id_type!r}")
    header = NM_HEADER_FIXED + PUBLISHER_ID_BYTES[publisher_id_type]
    return header + n_dsm * DSM_HEADER + sum(field_size(t) for t in types)


def frame_size(nm: int, vlan_tag: bool = True) -> FrameSize:
    ip_packet = IPV4_HEADER + UDP_HEADER + nm
    if ip_packet > MTU:
        raise SizeError(f"IP packet of {ip_packet} bytes exceeds the MTU of {MTU} (A5)")
    frame = max(MIN_FRAME, ETH_HEADER + (VLAN_TAG if vlan_tag else 0) + ip_packet + FCS)
    return FrameSize(nm=nm, ip_packet=ip_packet, frame=frame, wire=frame + PREAMBLE_SFD + IFG)
