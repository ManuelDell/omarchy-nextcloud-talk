.pragma library

// A deliberately small Markdown renderer. Raw HTML is always text.
var WRAPS = { bold: "**", italic: "*", strike: "~~", mono: "```", code: "`" }
var PREFIXES = { bullet: "- ", numbered: "", quote: "> " }

function escapeHtml(value) {
  return String(value).replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;")
}
function safeLink(value) {
  return /^(https?:\/\/|mailto:)[^\s<>"'\u0000-\u001f]+$/i.test(String(value || ""))
}
function tone(value, fallback) { return /^#[0-9a-f]{6}(?:[0-9a-f]{2})?$/i.test(String(value || "")) ? value : fallback }
function isWordChar(ch) { return !!ch && /[0-9A-Za-zÀ-ÖØ-öø-˿Ͱ-῿Ⰰ-퟿]/.test(ch) }
function inline(text, depth) {
  var level = depth || 0
  if (level > 16) return escapeHtml(text)
  var out = ""
  for (var i = 0; i < text.length;) {
    var rest = text.substring(i)
    var code = /^`([^`\n]+)`/.exec(rest)
    if (code) { out += "<code>" + escapeHtml(code[1]) + "</code>"; i += code[0].length; continue }
    var link = /^\[([^\]\n]+)\]\(([^)]*)\)/.exec(rest)
    if (link) {
      out += safeLink(link[2]) ? '<a href="' + escapeHtml(link[2]) + '">' + inline(link[1], level + 1) + '</a>'
        : escapeHtml(link[0])
      i += link[0].length
      continue
    }
    var url = /^(?:https?:\/\/|mailto:)[^\s<>"'`]+/i.exec(rest)
    if (url) {
      var target = url[0].replace(/[.,;!?)]+$/, "")
      if (safeLink(target)) {
        out += '<a href="' + escapeHtml(target) + '">' + escapeHtml(target) + '</a>'
        i += target.length
        continue
      }
    }
    var markers = ["**", "~~", "*", "_"]
    var matched = false
    for (var m = 0; m < markers.length; m++) {
      var marker = markers[m]
      if (rest.indexOf(marker) !== 0 || isWordChar(text[i - 1]) || /\s/.test(text[i + marker.length] || " ")) continue
      var end = text.indexOf(marker, i + marker.length)
      if (end <= i + marker.length || /\s/.test(text[end - 1])) continue
      var tag = marker === "**" ? "b" : marker === "~~" ? "s" : "i"
      out += '<' + tag + '>' + inline(text.substring(i + marker.length, end), level + 1) + '</' + tag + '>'
      i = end + marker.length
      matched = true
      break
    }
    if (matched) continue
    var mention = /^@[^\s@<>.,:;!?()[\]{}]+(?: [A-ZÀ-ÖØ-Þ][^\s@<>.,:;!?()[\]{}]*)*/.exec(rest)
    if (mention && !isWordChar(text[i - 1])) {
      out += '<b>' + escapeHtml(mention[0]) + '</b>'
      i += mention[0].length
      continue
    }
    if (text[i] === "\\" && /[*_~`\[\]\\]/.test(text[i + 1] || " ")) {
      out += escapeHtml(text[i + 1]); i += 2; continue
    }
    out += escapeHtml(text[i]); i++
  }
  return out
}
function toHtml(text, colors) {
  var palette = colors || {}
  var dim = tone(palette.dim, "#888888")
  var code = tone(palette.code, "#33888888")
  var lines = String(text || "").split("\n")
  var out = []
  var block = null
  for (var i = 0; i < lines.length; i++) {
    var line = lines[i]
    if (/^\s*```/.test(line)) {
      if (block !== null) {
        out.push('<span style="font-family: monospace; background-color: ' + code + '">' + escapeHtml(block.join("\n")).replace(/\n/g, "<br>") + '</span>')
        block = null
      } else block = []
      continue
    }
    if (block !== null) { block.push(line); continue }
    var bullet = /^\s*[-*] (.*)$/.exec(line)
    var numbered = /^\s*(\d+)\. (.*)$/.exec(line)
    var quote = /^> ?(.*)$/.exec(line)
    out.push(bullet ? '• ' + inline(bullet[1]) : numbered ? numbered[1] + '. ' + inline(numbered[2])
      : quote ? '<span style="color: ' + dim + '">▍ ' + inline(quote[1]) + '</span>' : inline(line))
  }
  if (block !== null) out.push('<code>' + escapeHtml(block.join("\n")).replace(/\n/g, '<br>') + '</code>')
  return '<span style="white-space: pre-wrap">' + out.join('<br>') + '</span>'
}
function hasFormatting(text) {
  return /[*_~`@]|https?:\/\/|mailto:|^\s*[-\d>]/m.test(String(text || ""))
}
function plain(text) {
  return String(text || "").replace(/^\s*```[^\n]*\n?/gm, "").replace(/`([^`]+)`/g, "$1")
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, "$1 ($2)")
    .replace(/\*\*(.+?)\*\*|~~(.+?)~~|\*([^*]+)\*|_([^_]+)_/g, function(match, bold, strike, italic, under) {
      return bold || strike || italic || under || ""
    })
}

// Selection formatting preserves the editor's undo history through apply().
function wrap(text, start, end, kind) {
  var marker = WRAPS[kind]
  var value = String(text || "")
  var from = Math.max(0, Math.min(start, end))
  var to = Math.min(value.length, Math.max(start, end))
  if (!marker) return { text: value, start: from, end: to }
  while (from < to && /\s/.test(value[from])) from++
  while (to > from && /\s/.test(value[to - 1])) to--
  var opening = kind === "mono" ? "```\n" : marker
  var closing = kind === "mono" ? "\n```" : marker
  var size = opening.length
  if (from >= size && value.substring(from - size, from) === opening && value.substring(to, to + closing.length) === closing)
    return { text: value.substring(0, from - size) + value.substring(from, to) + value.substring(to + closing.length), start: from - size, end: to - size }
  var lead = kind === "mono" && from > 0 && value[from - 1] !== "\n" ? "\n" : ""
  var tail = kind === "mono" && to < value.length && value[to] !== "\n" ? "\n" : ""
  return { text: value.substring(0, from) + lead + opening + value.substring(from, to) + closing + tail + value.substring(to), start: from + lead.length + size, end: to + lead.length + size }
}
function prefixLines(text, start, end, kind) {
  var value = String(text || "")
  var from = Math.max(0, Math.min(start, end))
  var to = Math.min(value.length, Math.max(start, end))
  var lineStart = value.lastIndexOf("\n", from - 1) + 1
  var nextBreak = value.indexOf("\n", to > from ? to - 1 : to)
  var lineEnd = nextBreak < 0 ? value.length : nextBreak
  var lines = value.substring(lineStart, lineEnd).split("\n")
  var pattern = kind === "numbered" ? /^\d+\. / : kind === "quote" ? /^> / : /^[-*] /
  var all = lines.every(function(line) { return pattern.test(line) })
  var changed = lines.map(function(line, index) {
    if (all) return line.replace(pattern, "")
    return (kind === "numbered" ? (index + 1) + ". " : PREFIXES[kind]) + line.replace(/^([-*] |\d+\. |> )/, "")
  }).join("\n")
  return { text: value.substring(0, lineStart) + changed + value.substring(lineEnd), start: lineStart, end: lineStart + changed.length }
}
function apply(text, start, end, kind) {
  var before = String(text || "")
  var result = ["bullet", "numbered", "quote"].indexOf(kind) >= 0 ? prefixLines(before, start, end, kind) : wrap(before, start, end, kind)
  var next = result.text
  if (next === before) return null
  var head = 0
  while (head < before.length && head < next.length && before[head] === next[head]) head++
  var tail = 0
  while (tail < before.length - head && tail < next.length - head && before[before.length - 1 - tail] === next[next.length - 1 - tail]) tail++
  return { head: head, end: before.length - tail, insert: next.substring(head, next.length - tail), start: result.start, selectEnd: result.end, text: next }
}
if (typeof module !== "undefined") module.exports = { toHtml: toHtml, hasFormatting: hasFormatting, plain: plain, wrap: wrap, prefixLines: prefixLines, escapeHtml: escapeHtml, safeLink: safeLink, apply: apply }
