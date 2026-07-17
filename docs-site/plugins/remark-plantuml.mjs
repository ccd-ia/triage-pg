// Build-time PlantUML → inline SVG, rendered by the LOCAL `plantuml` CLI.
//
// Every ```plantuml fence is piped through `plantuml -tsvg -pipe` (no network,
// no Kroki server) and replaced with the resulting SVG, wrapped in a centered
// <figure>. The root <svg> is made responsive (width/height stripped, viewBox
// kept) so diagrams scale to the content column in light/dark.
//
// Requires `plantuml` on PATH at build time (Java + Graphviz). Locally it's the
// Homebrew package; CI installs it in docs-pages.yml.
import { execFileSync } from 'node:child_process';
import { visit } from 'unist-util-visit';

function renderSvg(source) {
  const raw = execFileSync('plantuml', ['-tsvg', '-pipe'], {
    input: source,
    encoding: 'utf8',
    maxBuffer: 32 * 1024 * 1024,
  });
  // -pipe emits a bare <svg> (no XML prolog); slice defensively anyway.
  const start = raw.indexOf('<svg');
  if (start === -1) throw new Error('plantuml produced no <svg> output');
  let svg = raw.slice(start);
  // Make the ROOT <svg> tag responsive: drop fixed width/height (attrs + style),
  // keep viewBox for aspect ratio, let it scale to the container.
  svg = svg.replace(/<svg\b[^>]*>/, (tag) =>
    tag
      .replace(/\s(?:width|height)="[^"]*"/g, '')
      .replace(/preserveAspectRatio="[^"]*"/, 'preserveAspectRatio="xMidYMid meet"')
      .replace(/style="[^"]*"/, (s) => s.replace(/(?:width|height):[^;"]*;?/g, ''))
  );
  return svg;
}

export default function remarkPlantuml() {
  return (tree, file) => {
    visit(tree, 'code', (node, index, parent) => {
      if (node.lang !== 'plantuml' || !parent || index === null) return;
      let svg;
      try {
        svg = renderSvg(node.value);
      } catch (err) {
        // Fail loud but don't crash the whole site build on one bad diagram —
        // leave the source fence visible so it's obvious in review.
        console.error(
          `[remark-plantuml] failed to render a diagram in ${file.path ?? 'unknown file'}: ${err.message}`
        );
        return;
      }
      const html = {
        type: 'html',
        value: `<figure class="plantuml">${svg}</figure>`,
      };
      parent.children.splice(index, 1, html);
    });
  };
}
