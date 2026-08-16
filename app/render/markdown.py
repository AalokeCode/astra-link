"""Small, dependency-free Markdown renderer for generated documents."""

from __future__ import annotations

import html
import re


_STYLE = """
@page { margin: 22mm; }
body { color: #222; font: 16px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       margin: 0 auto; max-width: 850px; }
h1, h2, h3, h4, h5, h6 { line-height: 1.2; margin: 1.4em 0 .55em; }
p { margin: .7em 0; } blockquote { border-left: 4px solid #bbb; color: #555; margin: 1em 0;
       padding: .15em 1em; }
code, pre { background: #f4f4f4; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
code { border-radius: 3px; padding: .12em .3em; } pre { border: 1px solid #ddd; border-radius: 5px;
       overflow-wrap: break-word; padding: 1em; white-space: pre-wrap; }
pre code { background: none; padding: 0; } table { border-collapse: collapse; margin: 1em 0; width: 100%; }
th, td { border: 1px solid #bbb; padding: .45em .65em; text-align: left; }
th { background: #eee; } hr { border: 0; border-top: 1px solid #aaa; margin: 1.5em 0; }
a { color: #075ea8; } del { color: #666; }
""".strip()


def _inline(text: str) -> str:
    """Escape text, then add inline markup while preserving code spans."""
    parts = re.split(r"(`[^`\n]+`)", text)
    rendered: list[str] = []
    for part in parts:
        if part.startswith("`") and part.endswith("`"):
            rendered.append(f"<code>{html.escape(part[1:-1])}</code>")
            continue
        value = html.escape(part, quote=True)
        value = re.sub(
            r"\[([^]\n]+)\]\(([^)\n]+)\)",
            lambda match: f'<a href="{match.group(2)}">{match.group(1)}</a>',
            value,
        )
        value = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: f"<strong>{m.group(1) or m.group(2)}</strong>", value)
        value = re.sub(r"~~(.+?)~~", r"<del>\1</del>", value)
        value = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)|(?<!_)_([^_\n]+)_(?!_)", lambda m: f"<em>{m.group(1) or m.group(2)}</em>", value)
        rendered.append(value)
    return "".join(rendered)


def _split_table_row(line: str) -> list[str]:
    stripped = line.strip().strip("|")
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", stripped)]


def _table_separator(line: str) -> list[str] | None:
    cells = _split_table_row(line)
    if not cells or any(re.fullmatch(r":?-{3,}:?", cell) is None for cell in cells):
        return None
    return cells


def _alignment(separator: str) -> str:
    if separator.startswith(":") and separator.endswith(":"):
        return "center"
    if separator.endswith(":"):
        return "right"
    return "left"


def _is_block_start(lines: list[str], index: int) -> bool:
    line = lines[index]
    stripped = line.strip()
    if not stripped:
        return True
    if re.match(r"^(#{1,6})\s+", stripped) or re.match(r"^(```|~~~)", stripped):
        return True
    if re.match(r"^ {0,3}([-+*])\s+", line) or re.match(r"^ {0,3}\d+[.)]\s+", line):
        return True
    if line.startswith("    ") or stripped.startswith(">"):
        return True
    if re.fullmatch(r"(?:-{3,}|\*{3,}|_{3,})", stripped):
        return True
    return index + 1 < len(lines) and "|" in line and _table_separator(lines[index + 1]) is not None


def _render_list(lines: list[str], start: int) -> tuple[str, int]:
    first = re.match(r"^(\s*)(?:([-+*])|(\d+)[.)])\s+(.+)$", lines[start])
    assert first is not None
    root_indent = len(first.group(1).expandtabs(4))
    root_tag = "ol" if first.group(3) else "ul"
    output = [f"<{root_tag}>"]
    nested_tag: str | None = None
    index = start
    while index < len(lines):
        match = re.match(r"^(\s*)(?:([-+*])|(\d+)[.)])\s+(.+)$", lines[index])
        if match is None:
            break
        indent = len(match.group(1).expandtabs(4))
        tag = "ol" if match.group(3) else "ul"
        if indent < root_indent or indent == root_indent and tag != root_tag:
            break
        if indent > root_indent:
            if nested_tag is None:
                nested_tag = tag
                output.append(f"<{tag}>")
            elif tag != nested_tag:
                output.extend((f"</{nested_tag}>", f"<{tag}>"))
                nested_tag = tag
            output.append(f"<li>{_inline(match.group(4))}</li>")
        else:
            if nested_tag is not None:
                output.append(f"</{nested_tag}>")
                nested_tag = None
            output.append(f"<li>{_inline(match.group(4))}</li>")
        index += 1
    if nested_tag is not None:
        output.append(f"</{nested_tag}>")
    output.append(f"</{root_tag}>")
    return "\n".join(output), index


def markdown_to_html(text: str, *, title: str = "Document") -> str:
    """Return a complete standalone HTML5 document."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped:
            index += 1
            continue

        fence = re.match(r"^\s*(```|~~~)\s*([\w.+-]*)\s*$", line)
        if fence:
            marker, language = fence.groups()
            code: list[str] = []
            index += 1
            while index < len(lines) and re.match(rf"^\s*{re.escape(marker)}\s*$", lines[index]) is None:
                code.append(lines[index])
                index += 1
            if index < len(lines):
                index += 1
            class_name = f' class="language-{html.escape(language)}"' if language else ""
            blocks.append(f"<pre><code{class_name}>{html.escape(chr(10).join(code))}</code></pre>")
            continue

        if line.startswith("    "):
            code = []
            while index < len(lines) and (lines[index].startswith("    ") or not lines[index].strip()):
                code.append(lines[index][4:] if lines[index].startswith("    ") else "")
                index += 1
            blocks.append(f"<pre><code>{html.escape(chr(10).join(code).rstrip())}</code></pre>")
            continue

        heading = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", stripped)
        if heading:
            level = len(heading.group(1))
            blocks.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            index += 1
            continue

        if re.fullmatch(r"(?:-{3,}|\*{3,}|_{3,})", stripped):
            blocks.append("<hr>")
            index += 1
            continue

        if stripped.startswith(">"):
            quoted: list[str] = []
            while index < len(lines) and lines[index].lstrip().startswith(">"):
                quoted.append(re.sub(r"^\s*>\s?", "", lines[index]))
                index += 1
            blocks.append("<blockquote>" + "<br>\n".join(_inline(item) for item in quoted) + "</blockquote>")
            continue

        list_match = re.match(r"^\s*(?:[-+*]|\d+[.)])\s+", line)
        if list_match:
            rendered, index = _render_list(lines, index)
            blocks.append(rendered)
            continue

        if index + 1 < len(lines) and "|" in line:
            separators = _table_separator(lines[index + 1])
            headers = _split_table_row(line)
            if separators is not None and len(headers) == len(separators):
                aligns = [_alignment(cell) for cell in separators]
                rows: list[list[str]] = []
                index += 2
                while index < len(lines) and "|" in lines[index] and lines[index].strip():
                    row = _split_table_row(lines[index])
                    rows.append((row + [""] * len(headers))[: len(headers)])
                    index += 1
                table = ["<table>", "<thead><tr>"]
                table.extend(f'<th style="text-align:{align}">{_inline(cell)}</th>' for cell, align in zip(headers, aligns))
                table.extend(("</tr></thead>", "<tbody>"))
                for row in rows:
                    table.append("<tr>")
                    table.extend(f'<td style="text-align:{align}">{_inline(cell)}</td>' for cell, align in zip(row, aligns))
                    table.append("</tr>")
                table.extend(("</tbody>", "</table>"))
                blocks.append("\n".join(table))
                continue

        paragraph = [stripped]
        index += 1
        while index < len(lines) and not _is_block_start(lines, index):
            paragraph.append(lines[index].strip())
            index += 1
        blocks.append(f"<p>{_inline(' '.join(paragraph))}</p>")

    safe_title = html.escape(title)
    body = "\n".join(blocks)
    return f"<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n<title>{safe_title}</title>\n<style>{_STYLE}</style>\n</head>\n<body>\n{body}\n</body>\n</html>\n"
