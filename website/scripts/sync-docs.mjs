// Copies the public repository documents into docs/ so the site cannot drift from them.
// Relative links are rewritten to the GitHub blob URL, because the site does not contain the other repository files.
import { mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = join(here, "..", "..");
const docsDir = join(here, "..", "docs");
const blobBase = "https://github.com/Theseus-Guard/osaa-safe-starter-kit/blob/main/";

const pages = [
  { source: "README.md", target: "overview.md", title: "Overview", slug: "/", position: 1 },
  { source: "ROADMAP.md", target: "roadmap.md", title: "Roadmap", position: 2 },
  { source: "CHANGELOG.md", target: "changelog.md", title: "Changelog", position: 3 },
  { source: "SECURITY.md", target: "security-policy.md", title: "Security policy", position: 4 },
  { source: "CONTRIBUTING.md", target: "contributing.md", title: "Contributing", position: 5 },
  { source: "CODE_OF_CONDUCT.md", target: "code-of-conduct.md", title: "Code of conduct", position: 6 },
];

function rewriteLinks(markdown) {
  return markdown.replace(/\]\(([^)\s]+)\)/g, (match, href) => {
    if (/^(https?:|mailto:|#|\/)/.test(href)) return match;
    return `](${blobBase}${href})`;
  });
}

rmSync(docsDir, { recursive: true, force: true });
mkdirSync(docsDir, { recursive: true });

for (const page of pages) {
  const body = rewriteLinks(readFileSync(join(repoRoot, page.source), "utf8"));
  const frontMatter = [
    "---",
    `title: ${JSON.stringify(page.title)}`,
    ...(page.slug ? [`slug: ${page.slug}`] : []),
    `sidebar_position: ${page.position}`,
    "---",
    "",
    "",
  ].join("\n");
  writeFileSync(join(docsDir, page.target), frontMatter + body);
}
console.log(`Synced ${pages.length} documents into docs/.`);
