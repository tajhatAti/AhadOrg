"""
snippet_page.py — Builds the PUBLISHED standalone static page for a shared
snippet, GitHub-Pages style.

Key principle (per spec): a published page is a FINISHED, standalone page —
NOT a tool/editor interface. So:
  * NO Copy-code button, NO Download, NO View-source, NO tabs,
    NO console panel, NO fullscreen toggle, NO editor chrome.
  * Just the rendered output.

For HTML snippets we return the user's HTML VERBATIM as the response — a true
standalone static page (exactly like deploying index.html to GitHub Pages).
For other languages we render a clean, minimal viewer that just shows/runs the
content with zero developer-facing UI.
"""
import json
from datetime import datetime


# Languages that render live (the others are shown as plain text).
RUNNABLE = {"css", "javascript", "js", "markdown", "md"}


def is_html(lang: str) -> bool:
    return (lang or "text").lower() == "html"


def build_published_page(row):
    """Return the standalone HTML to serve for a published snippet.

    Returns (html_str, is_raw_html). When is_raw_html is True the caller should
    serve the string as-is (it's the user's own HTML document) — a true
    standalone static page.
    """
    lang = (row["language"] or "text").lower()
    content = row["content"] or ""

    # HTML -> the user's own document, verbatim. True standalone page.
    if is_html(lang):
        return content, True

    # Everything else -> a single clean viewer page (no tool UI).
    return _clean_viewer(row, lang, content), False


def _esc(s: str) -> str:
    return (str(s or "")
            .replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _fmt_date(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%b %d, %Y")
    except Exception:
        return ""


# The page carries the content as a JSON blob (never interpolated into
# HTML/JS) and the page's own JS renders it into a sandboxed iframe.
def _clean_viewer(row, lang, content):
    title = row["title"] or "Published page"
    created = _fmt_date(row["created_at"])
    data = json.dumps({
        "title": title,
        "language": lang,
        "content": content,
        "runnable": lang in RUNNABLE,
    })
    page = _VIEWER.replace("__TITLE__", _esc(title))
    page = page.replace("__DATA__", data)
    return page


_VIEWER = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>__TITLE__</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400&display=swap" rel="stylesheet">
<style>
  *{margin:0;padding:0;box-sizing:border-box;}
  html,body{height:100%;}
  body{background:#fff;}
  .stage{position:fixed;inset:0;}
  .stage iframe{width:100%;height:100%;border:none;background:#fff;}
  /* non-runnable (python, json, etc.) — clean monospace read */
  .read{
    font-family:'JetBrains Mono',monospace;font-size:14px;line-height:1.75;
    white-space:pre-wrap;word-break:break-word;padding:32px;max-width:920px;
    margin:0 auto;color:#1f2937;background:#fff;min-height:100vh;
  }
  /* a nearly-invisible attribution — finished product, not a tool */
  .attr{
    position:fixed;right:10px;bottom:8px;font-family:'Inter',sans-serif;
    font-size:10px;color:rgba(0,0,0,.18);text-decoration:none;letter-spacing:.3px;
    z-index:5;pointer-events:none;
  }
  .attr.rep{left:10px;right:auto;pointer-events:auto;}
  .attr.rep:hover{color:rgba(0,0,0,.45);}
  @media (prefers-color-scheme: dark){
    body,.read{background:#0b0d14;}.read{color:#d4d8e8;}.attr{color:rgba(255,255,255,.14);}
  }
</style>
</head>
<body>
<script type="application/json" id="data">__DATA__</script>
<div class="stage" id="stage"></div>
<a class="attr" href="/" tabindex="-1">Ahad Co</a>
<a class="attr rep" id="reportLink" href="/report-abuse" tabindex="-1">Report abuse</a>
<script>document.getElementById('reportLink').href='/report-abuse?url='+encodeURIComponent(location.href);</script>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<script>
(function(){
  var D = JSON.parse(document.getElementById('data').textContent);
  var body = D.content || '';
  var lang = (D.language||'text').toLowerCase();
  var stage = document.getElementById('stage');

  function srcdoc(){
    if (lang === 'css'){
      return '<!DOCTYPE html><html><head><meta charset="utf-8"><style>' + body + '</style></head>' +
        '<body style="font-family:system-ui,sans-serif;padding:32px;color:#111;background:#fff">' +
        '<h1>Heading</h1><p>Paragraph to show your <strong>CSS</strong>. <a href="#">A link</a>.</p>' +
        '<button>Button</button><ul><li>Item one</li><li>Item two</li></ul><input placeholder="Input"></body></html>';
    }
    if (lang === 'markdown' || lang === 'md'){
      var html = window.marked ? marked.parse(body) : ('<pre>' + body + '</pre>');
      return '<!DOCTYPE html><html><head><meta charset="utf-8"><style>' +
        'body{font-family:system-ui,-apple-system,sans-serif;padding:40px;max-width:760px;margin:0 auto;color:#1a1a2e;line-height:1.7;background:#fff}' +
        'h1,h2,h3{margin:1.2em 0 .5em}code{background:#f1f1f4;padding:2px 6px;border-radius:4px;font-size:.9em}' +
        'pre{background:#f4f4f8;padding:14px;border-radius:8px;overflow:auto}pre code{background:none;padding:0}' +
        'a{color:#6366f1}blockquote{border-left:3px solid #ddd;margin:0;padding-left:14px;color:#555}' +
        'img{max-width:100%}table{border-collapse:collapse}td,th{border:1px solid #ddd;padding:6px 10px}' +
        '</style></head><body>' + html + '</body></html>';
    }
    if (lang === 'javascript' || lang === 'js'){
      // run user JS inside the iframe (sandboxed), no console panel — just runs.
      var safe = body.split('<\\/script>').join('<\\\\/script>');
      return '<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>' +
        '<scr' + 'ipt>try{\\n' + safe + '\\n}catch(e){document.body.innerHTML="<pre style=\\"font:14px monospace;color:#b91c1c;padding:20px\\">"+e.message+"</pre>"}<' + '/scr' + 'ipt></body></html>';
    }
    return '';
  }

  if (D.runnable){
    var f = document.createElement('iframe');
    f.setAttribute('sandbox','allow-scripts allow-modals');
    f.srcdoc = srcdoc();
    stage.appendChild(f);
  } else {
    // plain-language snippet (python, json, bash ...) -> clean read-only text
    stage.innerHTML = '<div class="read"></div>';
    stage.firstChild.textContent = body;
  }
})();
</script>
</body>
</html>"""
