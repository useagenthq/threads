import { quote } from "@threads/core/adapter";

// Daytona's log stream marks stdout and stderr in-band with the bytes 01 01 01 and 02 02 02,
// and documents no escaping, so output holding those bytes can't be told from a marker
//. Each stream is therefore hex-encoded by `od` inside the sandbox before it
// reaches that channel, and decoded back to its exact bytes here. The wrapper keeps the
// command's own exit code.

/** Runs `script` with stdout and stderr each written as `od` hex on its own stream. */
export function framed(script: string): string {
  return [
    ": threads-framed",
    "d=$(mktemp -d) || exit 125",
    'mkfifo "$d/o" "$d/e" || exit 125',
    'od -An -tx1 -v < "$d/o" & o=$!',
    'od -An -tx1 -v < "$d/e" >&2 & e=$!',
    `sh -c ${quote(script)} > "$d/o" 2> "$d/e"; s=$?`,
    'wait "$o" "$e"; rm -rf "$d"; exit "$s"',
  ].join("\n");
}

const HEX = /[0-9a-f]/;

/** Turns a stream of `od` hex text back into bytes as they arrive. */
export function unhex(sink: (bytes: Uint8Array) => void): {
  readonly push: (chunk: Uint8Array) => void;
} {
  let odd = "";
  const text = new TextDecoder();
  return {
    push: (chunk) => {
      let digits = odd;
      for (const c of text.decode(chunk)) if (HEX.test(c)) digits += c;
      const even = digits.length - (digits.length % 2);
      odd = digits.slice(even);
      if (even > 0) sink(Uint8Array.fromHex(digits.slice(0, even)));
    },
  };
}
