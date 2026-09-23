import { generateOGImage } from "fumadocs-ui/og";
import type { ImageResponse } from "next/og";
import { LogoMark } from "@/components/logo";
import { appName } from "./shared";

/** The branded 1200x630 card used for the home page and every docs page. */
export function brandImage(title: string, description?: string): ImageResponse {
  return generateOGImage({
    title,
    description,
    site: appName,
    icon: <LogoMark width={55} height={48} color="#60A5FA" />,
    primaryColor: "rgba(37, 99, 235, 0.35)",
    primaryTextColor: "#60A5FA",
  });
}
