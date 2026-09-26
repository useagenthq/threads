import { generateOGImage } from "fumadocs-ui/og";
import type { ImageResponse } from "next/og";
import { LogoMark } from "@/components/logo";
import { appName } from "./shared";

/**
 * The branded 1200x630 card used for the home page and every docs page.
 * Satori cannot read CSS variables, so this is the one place that repeats the brand blue:
 * #2473FE is --brand and #2473FE is --brand-lift, both from app/theme.css. The card is drawn
 * on #0c0c0c, so the text uses the lifted step.
 */
export function brandImage(title: string, description?: string): ImageResponse {
  return generateOGImage({
    title,
    description,
    site: appName,
    icon: <LogoMark width={55} height={48} color="#2473FE" />,
    primaryColor: "rgba(26, 101, 254, 0.35)",
    primaryTextColor: "#2473FE",
  });
}
