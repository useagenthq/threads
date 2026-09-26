import type { BaseLayoutProps } from "fumadocs-ui/layouts/shared";
import { FlaskConical, GraduationCap, Rocket } from "lucide-react";
import { Logo } from "@/components/logo";
import { githubUrl } from "./shared";

// The header sections. They mirror the docs roots (Guides, Learn, API Reference, HTTP API) plus
// the Integrations catalogue, which lives in the Guides tree. Docs pages hide these and show the
// root tabs instead (app/docs/layout.tsx), so this set is what the landing page carries.
export function baseOptions(): BaseLayoutProps {
  return {
    nav: {
      title: <Logo />,
      url: "/",
    },
    githubUrl,
    links: [
      { text: "Docs", url: "/docs", active: "nested-url" },
      {
        type: "menu",
        text: "Learn",
        url: "/docs/learn",
        items: [
          {
            text: "Quickstart",
            url: "/docs/quickstart",
            icon: <Rocket />,
            description: "Run an agent in a minute, with no API key.",
          },
          {
            text: "Tutorials",
            url: "/docs/learn",
            icon: <GraduationCap />,
            description: "A path from your first agent to a team of them.",
          },
          {
            text: "Examples",
            url: "/docs/learn/examples",
            icon: <FlaskConical />,
            description: "Runnable samples that ship in the repository.",
          },
        ],
      },
      { text: "Integrations", url: "/docs/integrations" },
      // /docs/reference has no index page; overview is the folder's landing page.
      {
        text: "Reference",
        url: "/docs/reference/overview",
        active: "nested-url",
      },
    ],
  };
}
