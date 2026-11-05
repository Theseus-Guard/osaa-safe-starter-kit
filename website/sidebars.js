// @ts-check

// The root documents are listed one by one, so the user guide pages (docs/user-guide) appear once, in their own section.
/** @type {import('@docusaurus/plugin-content-docs').SidebarsConfig} */
const sidebars = {
  docs: [
    "overview",
    {
      type: "category",
      label: "User guide",
      link: { type: "doc", id: "user-guide/index" },
      collapsed: false,
      items: [
        "user-guide/concepts",
        "user-guide/quick-start",
        "user-guide/administrator-first-day",
        "user-guide/roles-and-permissions",
        "user-guide/screens",
        "user-guide/workflows",
        "user-guide/operator-guide",
        "user-guide/demo",
        "user-guide/faq",
      ],
    },
    "roadmap",
    "changelog",
    "security-policy",
    "contributing",
    "code-of-conduct",
  ],
};

module.exports = sidebars;
