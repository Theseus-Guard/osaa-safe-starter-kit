// @ts-check

/** @type {import('@docusaurus/types').Config} */
const config = {
  title: "SAFE-Starter-Kit",
  tagline: "A self-hosted starter kit for the SAFE workflow",
  url: "https://theseus-guard.github.io",
  baseUrl: "/osaa-safe-starter-kit/",
  organizationName: "Theseus-Guard",
  projectName: "osaa-safe-starter-kit",
  onBrokenLinks: "throw",
  markdown: { hooks: { onBrokenMarkdownLinks: "throw" } },
  i18n: { defaultLocale: "en", locales: ["en"] },

  presets: [
    [
      "classic",
      /** @type {import('@docusaurus/preset-classic').Options} */
      ({
        docs: {
          path: "docs",
          routeBasePath: "/",
          sidebarPath: "./sidebars.js",
          editUrl: "https://github.com/Theseus-Guard/osaa-safe-starter-kit/edit/main/",
        },
        blog: false,
        theme: {},
      }),
    ],
  ],

  themeConfig:
    /** @type {import('@docusaurus/preset-classic').ThemeConfig} */
    ({
      navbar: {
        title: "SAFE-Starter-Kit",
        items: [
          { type: "doc", docId: "overview", position: "left", label: "Overview" },
          { type: "doc", docId: "roadmap", position: "left", label: "Roadmap" },
          { type: "doc", docId: "changelog", position: "left", label: "Changelog" },
          {
            href: "https://github.com/Theseus-Guard/osaa-safe-starter-kit",
            label: "GitHub",
            position: "right",
          },
        ],
      },
      footer: {
        style: "dark",
        copyright:
          "SAFE-Starter-Kit is an independent open-source project. It is not an official Alliance product. Licensed under Apache-2.0.",
      },
    }),
};

module.exports = config;
