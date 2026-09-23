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

function v6Public(ip: string): boolean {
  const h = hextets(ip.split("%")[0] ?? ip);
  const [a = 0] = h;
  const embedded = (): string =>
    [
      (h[6] ?? 0) >> 8,
      (h[6] ?? 0) & 255,
      (h[7] ?? 0) >> 8,
      (h[7] ?? 0) & 255,
    ].join(".");
  const zeroPrefix = (n: number): boolean =>
    h.slice(0, n).every((x) => x === 0);
  // ::ffff:a.b.c.d (mapped), ::a.b.c.d (compatible) and 64:ff9b::/96 (NAT64) carry IPv4.
  if (zeroPrefix(5) && h[5] === 0xffff) return v4Public(embedded());
  if (zeroPrefix(6)) return h[6] !== 0 && v4Public(embedded());
  if (a === 0x64 && h[1] === 0xff9b && h.slice(2, 6).every((x) => x === 0))
    return v4Public(embedded());
  if ((a & 0xfe00) === 0xfc00) return false; // fc00::/7 ULA
  if ((a & 0xffc0) === 0xfe80) return false; // fe80::/10 link-local
  if ((a & 0xff00) === 0xff00) return false; // multicast
  if (a === 0x2001 && h[1] === 0x0db8) return false; // documentation
  return true;
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
