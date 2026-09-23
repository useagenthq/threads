// A document's passages with their UTF-8 byte spans (the wire Span, never code units): the
// paragraphs between blank lines, trimmed. The same split as the Python provider, so a shared
// store cites the same spans from either language.

export type Passage = {
  readonly start: number;
  readonly end: number;
  readonly text: string;
};

const encoder = new TextEncoder();
const utf8 = new TextDecoder("utf-8", { fatal: true });
const bytes = (s: string): number => encoder.encode(s).length;

/** The document's text, or undefined when its bytes are not UTF-8 (a visible parse failure). */
export function decodeText(content: Uint8Array): string | undefined {
  try {
    return utf8.decode(content);
  } catch {
    return undefined;
  }
}

export function passages(text: string): readonly Passage[] {
  const out: Passage[] = [];
  let start = 0;
  for (const found of [...text.matchAll(/\n\s*\n/g), undefined]) {
    const end = found === undefined ? text.length : found.index;
    const body = text.slice(start, end);
    const trimmed = body.trim();
    if (trimmed !== "") {
      const lead = body.length - body.trimStart().length;
      const first = bytes(text.slice(0, start + lead));
      out.push({ start: first, end: first + bytes(trimmed), text: trimmed });
    }
    if (found !== undefined) start = found.index + found[0].length;
  }
  return out;
}
