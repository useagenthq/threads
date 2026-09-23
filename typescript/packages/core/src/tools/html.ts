// web_fetch's page-to-text conversion: headings, links and list items keep
// their markdown shape; scripts, styles and every other tag are dropped.
// ponytail: regex conversion, no DOM; tables and nested markup flatten to text. A real
// HTML-to-markdown converter is a dependency decision for later.

const ENTITIES: Readonly<Record<string, string>> = {
  amp: "&",
  lt: "<",
  gt: ">",
  quot: '"',
  apos: "'",
  nbsp: " ",
};

function decode(text: string): string {
  return text.replace(
    /&(#x[0-9a-f]+|#\d+|[a-z]+);/gi,
    (whole, name: string) => {
      const lower = name.toLowerCase();
      if (lower.startsWith("#x"))
        return safeChar(Number.parseInt(lower.slice(2), 16), whole);
      if (lower.startsWith("#"))
        return safeChar(Number.parseInt(lower.slice(1), 10), whole);
      return ENTITIES[lower] ?? whole;
    },
  );
}

function safeChar(code: number, fallback: string): string {
  return code > 0 && code <= 0x10ffff ? String.fromCodePoint(code) : fallback;
}

export function htmlTitle(html: string): string | undefined {
  const found = /<title[^>]*>([\s\S]*?)<\/title>/i.exec(html)?.[1];
  const title = found === undefined ? "" : decode(found).trim();
  return title === "" ? undefined : title;
}

export function htmlToMarkdown(html: string): string {
  const text = html
    .replace(/<(script|style|noscript|template|svg|head)\b[\s\S]*?<\/\1>/gi, "")
    .replace(/<!--[\s\S]*?-->/g, "")
    .replace(
      /<h([1-6])[^>]*>([\s\S]*?)<\/h\1>/gi,
      (_, level: string, body: string) =>
        `\n\n${"#".repeat(Number(level))} ${body.trim()}\n\n`,
    )
    .replace(
      /<a\b[^>]*href\s*=\s*["']([^"']*)["'][^>]*>([\s\S]*?)<\/a>/gi,
      (_, href: string, body: string) => `[${body.trim()}](${href})`,
    )
    .replace(/<li\b[^>]*>/gi, "\n- ")
    .replace(/<(br|hr)\b[^>]*>/gi, "\n")
    .replace(
      /<\/?(p|div|section|article|ul|ol|table|tr|pre|blockquote)\b[^>]*>/gi,
      "\n\n",
    )
    .replace(/<[^>]+>/g, "");
  return decode(text)
    .split("\n")
    .map((line) => line.replace(/[ \t]+/g, " ").trim())
    .join("\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}
