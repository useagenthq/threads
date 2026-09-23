import type { BaseLayoutProps } from "fumadocs-ui/layouts/shared";
import { Logo } from "@/components/logo";
import { githubUrl } from "./shared";

export function baseOptions(): BaseLayoutProps {
  return {
    nav: {
      title: <Logo />,
      url: "/",
    },
    githubUrl,
    links: [{ text: "Docs", url: "/docs", active: "nested-url" }],
  };
}
