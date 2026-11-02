// Copies the public repository documents into docs/ so the site cannot drift from them.
// Relative links are rewritten to the GitHub blob URL, because the site does not contain the other repository files.
// The user guide pages (docs/user-guide in the repository) are copied into docs/user-guide. Links between them stay
// relative, so Docusaurus resolves them. Links to any other repository file are rewritten to the GitHub blob URL.
import { copyFileSync, mkdirSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join, posix } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = join(here, "..", "..");
const docsDir = join(here, "..", "docs");
const blobBase = "https://github.com/Theseus-Guard/osaa-safe-starter-kit/blob/main/";
const guideSourceDir = "docs/user-guide";

const pages = [
  { source: "README.md", target: "overview.md", title: "Overview", slug: "/", position: 1 },
  { source: "ROADMAP.md", target: "roadmap.md", title: "Roadmap", position: 2 },
  { source: "CHANGELOG.md", target: "changelog.md", title: "Changelog", position: 3 },
  { source: "SECURITY.md", target: "security-policy.md", title: "Security policy", position: 4 },
  { source: "CONTRIBUTING.md", target: "contributing.md", title: "Contributing", position: 5 },
  { source: "CODE_OF_CONDUCT.md", target: "code-of-conduct.md", title: "Code of conduct", position: 6 },
];

// Screenshots of the user guide are copied into docs/user-guide/images. The README refers to them by their repository
// path, which the site serves from its user-guide folder.
const imagePattern = /\.(png|jpe?g|gif|svg)$/i;

function rewriteLinks(markdown) {
  return markdown.replace(/\]\(([^)\s]+)\)/g, (match, href) => {
    if (/^(https?:|mailto:|#|\/)/.test(href)) return match;
    const guideImage = href.match(/^docs\/user-guide\/(images\/[^/]+)$/);
    if (guideImage && imagePattern.test(href)) return `](user-guide/${guideImage[1]})`;
    return `](${blobBase}${href})`;
  });
}

// A link to another guide page is a bare file name, or ./file.md, and stays relative. Any other relative link is
// resolved from docs/user-guide and points at the GitHub file.
function rewriteGuideLinks(markdown) {
  return markdown.replace(/\]\(([^)\s]+)\)/g, (match, href) => {
    if (/^(https?:|mailto:|#|\/)/.test(href)) return match;
    const [path, anchor] = href.split("#");
    if (/^(\.\/)?[^/]+\.md$/.test(path)) return match;
    if (/^images\/[^/]+$/.test(path) && imagePattern.test(path)) return match;
    const repoPath = posix.normalize(posix.join(guideSourceDir, path));
    return `](${blobBase}${repoPath}${anchor === undefined ? "" : `#${anchor}`})`;
  });
}

function titleOf(markdown, fallback) {
  const heading = markdown.match(/^# (.+)$/m);
  return heading ? heading[1].trim() : fallback;
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

const guideFiles = readdirSync(join(repoRoot, guideSourceDir))
  .filter((name) => name.endsWith(".md"))
  .sort();
if (guideFiles.length === 0) {
  throw new Error(`No user guide pages found in ${guideSourceDir}.`);
}
const guideTargetDir = join(docsDir, "user-guide");
mkdirSync(guideTargetDir, { recursive: true });
for (const name of guideFiles) {
  const body = rewriteGuideLinks(readFileSync(join(repoRoot, guideSourceDir, name), "utf8"));
  const frontMatter = ["---", `title: ${JSON.stringify(titleOf(body, name))}`, "---", "", ""].join("\n");
  writeFileSync(join(guideTargetDir, name), frontMatter + body);
}

const guideImageDir = join(repoRoot, guideSourceDir, "images");
const guideImages = readdirSync(guideImageDir).filter((name) => imagePattern.test(name));
mkdirSync(join(guideTargetDir, "images"), { recursive: true });
for (const name of guideImages) {
  copyFileSync(join(guideImageDir, name), join(guideTargetDir, "images", name));
}

console.log(
  `Synced ${pages.length} documents, ${guideFiles.length} user guide pages and ${guideImages.length} images into docs/.`,
);
