import { DocsLayout } from "fumadocs-ui/layouts/notebook";
import type { ReactNode } from "react";
import { LangSwitch } from "@/components/lang-switch";
import { baseOptions } from "@/lib/layout.shared";
import { source } from "@/lib/source";

// Notebook layout: a full-width top bar with the section tabs, sidebar below it.
export default function Layout({ children }: { children: ReactNode }) {
  const base = baseOptions();
  return (
    <DocsLayout
      tree={source.getPageTree()}
      {...base}
      nav={{ ...base.nav, mode: "top", children: <LangSwitch /> }}
      tabMode="navbar"
      links={[]}
    >
      {children}
    </DocsLayout>
  );
}
