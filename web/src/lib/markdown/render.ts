/**
 * Safe rich text for model output: Markdown + LaTeX math + fenced code.
 *
 *   1. protect math   — pull $$…$$, \[…\], \(…\), $…$ out into inert sentinels so
 *                       Markdown never mangles a backslash/caret/underscore.
 *   2. Markdown → HTML (markdown-it; raw HTML allowed, then…)
 *   3. sanitize       — DOMPurify scrubs the untrusted HTML.
 *   4. restore math   — KaTeX output (trusted: we produce it) after sanitising.
 *
 * Bad LaTeX renders as red source; an unclosed delimiter mid-stream simply stays
 * text until the closing token arrives. This module is heavy, so it's only ever
 * imported lazily (see `RichText`).
 */
import DOMPurify from "dompurify";
import hljs from "highlight.js/lib/core";
import bash from "highlight.js/lib/languages/bash";
import javascript from "highlight.js/lib/languages/javascript";
import json from "highlight.js/lib/languages/json";
import python from "highlight.js/lib/languages/python";
import katex from "katex";
import MarkdownIt from "markdown-it";
import "katex/dist/katex.min.css";

hljs.registerLanguage("python", python);
hljs.registerLanguage("javascript", javascript);
hljs.registerLanguage("js", javascript);
hljs.registerLanguage("json", json);
hljs.registerLanguage("bash", bash);

const md: MarkdownIt = new MarkdownIt({
  html: true,
  linkify: true,
  breaks: true,
  highlight(code, lang) {
    const body = lang && hljs.getLanguage(lang)
      ? hljs.highlight(code, { language: lang, ignoreIllegals: true }).value
      : md.utils.escapeHtml(code);
    return `<pre class="hljs"><code>${body}</code></pre>`;
  },
});

const OPEN = "", CLOSE = "";
const RULES: { re: RegExp; display: boolean }[] = [
  { re: /\$\$([\s\S]+?)\$\$/g, display: true },
  { re: /\\\[([\s\S]+?)\\\]/g, display: true },
  { re: /```(?:math|latex|tex)\n([\s\S]+?)```/g, display: true },
  { re: /\\\(([\s\S]+?)\\\)/g, display: false },
  { re: /\$(?![ \t])([^$\n]+?)(?<![ \t])\$/g, display: false },
];

const escapeHtml = (s: string) => s.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" })[c]!);

/** Markdown + math → sanitized HTML. Citation markers `[n]` become `<a class="cite" data-n>`. */
export function renderRich(src: string, { citations = false } = {}): string {
  const store: { tex: string; display: boolean }[] = [];
  let text = String(src ?? "");
  for (const { re, display } of RULES) {
    text = text.replace(re, (_, tex: string) => `${OPEN}${store.push({ tex, display }) - 1}${CLOSE}`);
  }
  let html = DOMPurify.sanitize(md.render(text), { ADD_ATTR: ["target", "rel"] });
  html = html.replace(new RegExp(`${OPEN}(\\d+)${CLOSE}`, "g"), (_, i: string) => {
    const m = store[Number(i)];
    if (!m) return "";
    try {
      return katex.renderToString(m.tex, { displayMode: m.display, throwOnError: false, output: "htmlAndMathml", strict: false, trust: false });
    } catch {
      return escapeHtml(m.display ? `$$${m.tex}$$` : `$${m.tex}$`);
    }
  });
  const box = document.createElement("div");
  box.innerHTML = html;
  box.querySelectorAll<HTMLAnchorElement>("a[href]").forEach((a) => {
    if (/^https?:/i.test(a.getAttribute("href") ?? "")) { a.target = "_blank"; a.rel = "noopener noreferrer"; }
  });
  if (citations) linkifyCitations(box);
  return box.innerHTML;
}

const SKIP = new Set(["CODE", "PRE", "A", "SCRIPT", "STYLE"]);
/** `[3]` or a group like `[1, 2]` / `[1,2,5]`. */
const CITE = /\[(\d+(?:\s*,\s*\d+)*)\]/;

function linkifyCitations(root: HTMLElement) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      for (let p = node.parentElement; p && p !== root; p = p.parentElement) {
        if (SKIP.has(p.tagName) || p.classList.contains("katex")) return NodeFilter.FILTER_REJECT;
      }
      return CITE.test(node.nodeValue ?? "") ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP;
    },
  });
  const nodes: Text[] = [];
  for (let n = walker.nextNode(); n; n = walker.nextNode()) nodes.push(n as Text);
  for (const node of nodes) {
    const s = node.nodeValue ?? "";
    const frag = document.createDocumentFragment();
    let last = 0;
    for (const m of s.matchAll(new RegExp(CITE.source, "g"))) {
      if (m.index! > last) frag.append(s.slice(last, m.index));
      for (const n of m[1]!.split(",").map((x) => x.trim())) {
        const a = document.createElement("button");
        a.type = "button";
        a.className = "cite";
        a.dataset.n = n;
        a.textContent = n;
        a.setAttribute("aria-label", `Source ${n}`);
        frag.append(a);
      }
      last = m.index! + m[0].length;
    }
    if (last < s.length) frag.append(s.slice(last));
    node.replaceWith(frag);
  }
}
