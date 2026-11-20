#!/usr/bin/env python3
"""Build a standalone copy of index.html for static hosts (Vercel, Netlify, S3).

index.html is written for the Claude artifact host, which adds the <html>/<head>
wrapper and renders Mermaid diagrams itself. This script writes site/index.html
with a full document wrapper and the Mermaid library, themed to match the page.

Usage:  python3 business-idea-folder/build_site.py
Deploy: cd business-idea-folder/site && vercel --prod
"""
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "index.html"
OUT_DIR = HERE / "site"

HEAD_OPEN = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="description" content="Tradie Connect: an AI phone receptionist and crew line for Australian trade businesses.">
"""

EXTRA_CSS = """<style>
body{margin:0}
.diagram svg{max-width:100%;height:auto}
pre.mermaid:not([data-processed]){visibility:hidden;min-height:12rem}
</style>
"""

MERMAID = """
<script src="https://cdn.jsdelivr.net/npm/mermaid@10.9.1/dist/mermaid.min.js"></script>
<script>
(function () {
  var css = getComputedStyle(document.documentElement);
  var v = function (n) { return css.getPropertyValue(n).trim(); };
  mermaid.initialize({
    startOnLoad: false,
    securityLevel: "loose",
    theme: "base",
    fontFamily: v("--f-body"),
    themeVariables: {
      background: v("--surface"),
      primaryColor: v("--accent-soft"),
      primaryTextColor: v("--ink"),
      primaryBorderColor: v("--accent"),
      secondaryColor: v("--bg"),
      tertiaryColor: v("--surface"),
      lineColor: v("--muted"),
      textColor: v("--ink"),
      mainBkg: v("--accent-soft"),
      nodeBorder: v("--accent"),
      actorBkg: v("--accent-soft"),
      actorBorder: v("--accent"),
      actorTextColor: v("--ink"),
      actorLineColor: v("--muted"),
      signalColor: v("--ink"),
      signalTextColor: v("--ink"),
      labelBoxBkgColor: v("--surface"),
      labelTextColor: v("--ink"),
      noteBkgColor: v("--hivis"),
      noteTextColor: v("--hivis-ink"),
      fontSize: "15px"
    },
    flowchart: { htmlLabels: true, curve: "basis", useMaxWidth: true },
    sequence: {
      useMaxWidth: true, mirrorActors: false,
      messageFontSize: 15, actorFontSize: 15, noteFontSize: 14,
      actorMargin: 60, messageMargin: 40
    }
  });
  // Draw after web fonts load so label boxes are measured with the real font,
  // one diagram at a time (parallel renders can land in the wrong container).
  var draw = async function () {
    var els = Array.prototype.slice.call(document.querySelectorAll("pre.mermaid"));
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      var t = document.createElement("textarea");
      t.innerHTML = el.innerHTML;          // keeps <br/> labels, decodes --&gt;
      try {
        var out = await mermaid.render("mmd-" + i, t.value.trim());
        el.innerHTML = out.svg;
      } catch (e) {
        console.error("Mermaid diagram " + i + " failed", e);
      }
      el.setAttribute("data-processed", "true");
    }
  };
  (document.fonts && document.fonts.ready ? document.fonts.ready : Promise.resolve()).then(draw, draw);
})();
</script>
"""


def main() -> None:
    page = SRC.read_text(encoding="utf-8")

    # The artifact-only meta tag means nothing elsewhere.
    page = page.replace('<meta name="claude:viewport-fit-sides" content="cover">\n', "")

    # Everything up to the end of the <style> block is head content.
    marker = "</style>\n"
    split_at = page.index(marker) + len(marker)
    head, body = page[:split_at], page[split_at:]

    html = (
        HEAD_OPEN
        + head
        + EXTRA_CSS
        + "</head>\n<body>\n"
        + body.rstrip()
        + "\n"
        + MERMAID
        + "</body>\n</html>\n"
    )

    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / "index.html").write_text(html, encoding="utf-8")
    print(f"Wrote {OUT_DIR / 'index.html'}")


if __name__ == "__main__":
    main()
