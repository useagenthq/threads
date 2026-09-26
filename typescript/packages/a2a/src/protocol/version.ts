import { type A2aFault, fault } from "./errors";

// `A2A-Version` (pinned spec §5.5). We speak 1.0 and nothing else: a 0.3 peer is refused in both
// directions (decision 1). Clients MUST send the header and MAY send it as a query parameter
// instead, so inbound we read the header first and then the parameter. Both absent means 0.3 by
// the spec's own rule, which is VersionNotSupportedError, not a default to 1.0.

export const A2A_VERSION = "1.0";
export const VERSION_HEADER = "A2A-Version";
export const EXTENSIONS_HEADER = "A2A-Extensions";

/** The content type the HTTP+JSON binding prefers; `application/json` is accepted inbound. */
export const A2A_JSON = "application/a2a+json";

/**
 * The version a request declares, checked against ours. Only `Major.Minor` is compared, as the
 * spec requires, so `1.0.1` is 1.0 and `1.0` with trailing space is still 1.0.
 */
export function checkVersion(
  header: string | null,
  query: string | null,
): A2aFault | undefined {
  // A header that is present but empty is absent, so the query parameter is still read: the
  // spec lets a client send the version as a parameter INSTEAD of the header.
  const raw = (header ?? "").trim() || (query ?? "").trim();
  if (raw === "")
    return fault(
      "VersionNotSupportedError",
      `no ${VERSION_HEADER}, which the specification reads as 0.3; this agent speaks ${A2A_VERSION}`,
    );
  return majorMinor(raw) === A2A_VERSION
    ? undefined
    : fault(
        "VersionNotSupportedError",
        `${VERSION_HEADER} ${raw} is not supported; this agent speaks ${A2A_VERSION}`,
      );
}

/** `1.0.1` → `1.0`; anything that is not two or three dotted numbers stays as it came. */
export function majorMinor(version: string): string {
  const parts = version.split(".");
  const [major, minor] = parts;
  return major !== undefined &&
    minor !== undefined &&
    /^\d+$/.test(major) &&
    /^\d+$/.test(minor)
    ? `${major}.${minor}`
    : version;
}

/** A card interface's version, for picking the one we can speak. */
export function speaks1_0(protocolVersion: string): boolean {
  return majorMinor(protocolVersion) === A2A_VERSION;
}
