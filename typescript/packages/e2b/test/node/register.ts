import { existsSync } from "node:fs";
import { registerHooks } from "node:module";
import { fileURLToPath } from "node:url";

// Node resolves an ESM import only by its full file name; the sources, written for Bun and the
// bundler resolution tsconfig uses, import `./driver`, not `./driver.ts`. This resolves such a
// relative import to its .ts file, so Node's type stripping runs the sources unchanged.
// Loaded with `node --import`.

registerHooks({
  resolve: (specifier, context, next) => {
    const parent = context.parentURL;
    if (specifier.startsWith(".") && parent?.startsWith("file:"))
      for (const suffix of ["", ".ts", "/index.ts"]) {
        const url = new URL(specifier + suffix, parent);
        if (url.pathname.endsWith(".ts") && existsSync(fileURLToPath(url)))
          return next(url.href, context);
      }
    return next(specifier, context);
  },
});
