.pragma library

function listOf(value) {
  if (!value || typeof value === "string") return []
  var count = Number(value.length || 0)
  // One beyond the 12-option limit still lets compose report the count error.
  if (!isFinite(count) || count < 0 || count % 1 !== 0 || count > 13) return []
  var items = []
  for (var i = 0; i < count; i++) items.push(value[i])
  return items
}

// Python's limits count Unicode code points, not UTF-16 code units.
function textLength(value) {
  return value.replace(/[\uD800-\uDBFF][\uDC00-\uDFFF]/g, "_").length
}

function compose(question, options, multi, hidden) {
  var value = String(question || "").trim()
  var choices = listOf(options).map(function(option) { return String(option || "").trim() })
    .filter(function(option) { return option !== "" })
  var error = ""
  if (value === "") error = "Give the poll a question."
  else if (textLength(value) > 255) error = "The poll question must be at most 255 characters."
  else if (choices.length < 2 || choices.length > 12) error = "Add between 2 and 12 poll options."
  else if (choices.some(function(option) { return textLength(option) > 100 }))
    error = "Poll options must be at most 100 characters."
  else if (choices.some(function(option, index) {
    return choices.slice(0, index).some(function(previous) {
      return previous.toLowerCase() === option.toLowerCase()
    })
  })) error = "Poll options must be unique."
  var maximum = Number(multi)
  if (error === "" && (!isFinite(maximum) || maximum % 1 !== 0
      || maximum < 1 || maximum > choices.length))
    error = "Choose a valid poll selection limit."
  return { error: error, question: value, options: choices, multi: maximum, hidden: hidden === true }
}

// A tap toggles one's choice; [] deliberately withdraws the entire vote.
function nextSelection(poll, text) {
  if (!poll || poll.closed === true) return null
  var options = listOf(poll.options)
  var value = String(text || "")
  if (!options.some(function(option) { return String(option.text) === value })) return null
  var mine = options.filter(function(option) { return option.mine === true })
    .map(function(option) { return String(option.text) })
  var at = mine.indexOf(value)
  var maximum = Number(poll.selectable || 1)
  var next = maximum > 1
    ? (at >= 0 ? mine.filter(function(option) { return option !== value }) : mine.concat([value]))
    : (at >= 0 ? [] : [value])
  return next.length <= maximum ? next : null
}

function canClose(message, moderator) {
  return !!(message && message.poll && message.poll.closed !== true
    && message.pending !== true && message.revoked !== true && message.system !== true
    && (message.from_me === true || moderator === true))
}

function isModerator(details, account, jid) {
  if (!details || String(details.account || "") !== String(account || "")
      || !details.chat || String(details.chat.jid || "") !== String(jid || "")) return false
  var role = details.group ? String(details.group.my_role || "") : ""
  return role === "owner" || role === "moderator"
}

function writeError(result, fallback) {
  if (result && result.error) return String(result.error)
  return result && result.supported === false
    ? "Polls are not supported by this Talk server." : fallback
}
