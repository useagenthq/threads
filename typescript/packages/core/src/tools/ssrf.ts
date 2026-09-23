import { isIP } from "node:net";

// The web_fetch SSRF guard: only public unicast addresses. Private, loopback,
// link-local (the 169.254.169.254 metadata address among them), CGNAT, ULA, multicast and
// reserved ranges are denied, including IPv4 carried inside IPv6.

const V4_DENIED: readonly (readonly [number, number])[] = [
  [0x00000000, 8], // 0.0.0.0/8 "this network"
  [0x0a000000, 8], // 10/8
  [0x64400000, 10], // 100.64/10 CGNAT
  [0x7f000000, 8], // loopback
  [0xa9fe0000, 16], // link-local and cloud metadata
  [0xac100000, 12], // 172.16/12
  [0xc0000000, 24], // 192.0.0/24 IETF assignments
  [0xc0000200, 24], // TEST-NET-1
  [0xc0586300, 24], // 192.88.99/24 6to4 relay anycast
  [0xc0a80000, 16], // 192.168/16
  [0xc6120000, 15], // 198.18/15 benchmarking
  [0xc6336400, 24], // TEST-NET-2
  [0xcb007100, 24], // TEST-NET-3
  [0xe0000000, 3], // multicast, reserved and broadcast (224/3)
];

function v4(ip: string): number {
  return ip
    .split(".")
    .reduce((n, octet) => n * 256 + Number.parseInt(octet, 10), 0);
}

function v4Public(ip: string): boolean {
  const n = v4(ip);
  return !V4_DENIED.some(([base, bits]) => {
    const mask = bits === 0 ? 0 : (0xffffffff << (32 - bits)) >>> 0;
    return (n & mask) >>> 0 === base;
  });
}

/** The 8 hextets of an IPv6 address, with an embedded dotted IPv4 tail expanded. */
function hextets(ip: string): readonly number[] {
  const dotted = /(\d+\.\d+\.\d+\.\d+)$/.exec(ip);
  let text = ip;
  if (dotted?.[1] !== undefined) {
    const n = v4(dotted[1]);
    text = `${ip.slice(0, dotted.index)}${(n >>> 16).toString(16)}:${(n & 0xffff).toString(16)}`;
  }
  const [head = "", tail] = text.split("::");
  const parse = (s: string): number[] =>
    s === "" ? [] : s.split(":").map((h) => Number.parseInt(h, 16));
  const left = parse(head);
  const right = tail === undefined ? [] : parse(tail);
  return [
    ...left,
    ...Array<number>(8 - left.length - right.length).fill(0),
    ...right,
  ];
}

/** Denied inside 2000::/3: [first hextet, second hextet mask, second hextet value]. */
const V6_DENIED: readonly (readonly [number, number, number])[] = [
  [0x2001, 0xfe00, 0x0000], // 2001::/23 Teredo and IETF protocol assignments
  [0x2001, 0xffff, 0x0db8], // 2001:db8::/32 documentation
  [0x2002, 0x0000, 0x0000], // 2002::/16 6to4
  [0x3fff, 0xf000, 0x0000], // 3fff::/20 documentation
];

function v6Public(ip: string): boolean {
  const h = hextets(ip.split("%")[0] ?? ip);
  const [a = 0, b = 0] = h;
  const embedded = (): string =>
    [
      (h[6] ?? 0) >> 8,
      (h[6] ?? 0) & 255,
      (h[7] ?? 0) >> 8,
      (h[7] ?? 0) & 255,
    ].join(".");
  const zeros = (from: number, to: number): boolean =>
    h.slice(from, to).every((x) => x === 0);
  // Only IPv4-mapped ::ffff:0:0/96 and NAT64 64:ff9b::/96 are judged as their IPv4 address.
  if (zeros(0, 5) && h[5] === 0xffff) return v4Public(embedded());
  if (a === 0x64 && b === 0xff9b && zeros(2, 6)) return v4Public(embedded());
  // Everything outside global unicast 2000::/3 is denied (IPv4-compatible, ULA, link-local, ...).
  if ((a & 0xe000) !== 0x2000) return false;
  return !V6_DENIED.some(
    ([first, mask, value]) => a === first && (b & mask) === value,
  );
}

/** True only for a public unicast IP literal; anything unparseable is denied. */
export function isPublicAddress(ip: string): boolean {
  switch (isIP(ip)) {
    case 4:
      return v4Public(ip);
    case 6:
      return v6Public(ip);
    default:
      return false;
  }
}
