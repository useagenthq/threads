import { brandImage } from "@/lib/og";
import { appName, tagline } from "@/lib/shared";

export const revalidate = false;
export const alt = appName;
export const size = { width: 1200, height: 630 };
export const contentType = "image/png";

export default function Image() {
  return brandImage(appName, tagline);
}
