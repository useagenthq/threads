import { createFromSource } from "fumadocs-core/search/server";
import { source } from "@/lib/source";

// Exported as a static index: the browser downloads it and searches locally.
export const revalidate = false;
export const { staticGET: GET } = createFromSource(source);
