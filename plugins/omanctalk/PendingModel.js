.pragma library

// A fresh session namespace also separates sends from a persistent outbox.
function sessionPrefix() {
  var random = ""
  for (var i = 0; i < 4; i++)
    random += Math.floor(Math.random() * 0x100000000).toString(36).padStart(7, "0")
  return "pending-" + Date.now().toString(36) + "-" + random
}

function localId(session, serial) {
  return session + "-" + serial.toString(36)
}

function sendResult(payload, now) {
  return { state: payload.queued === true ? "queued" : "sent",
    message_id: String(payload.message_id || ""), created: now }
}

function mirrorIndex(messages) {
  var ids = Object.create(null)
  var references = Object.create(null)
  for (var i = 0; i < messages.length; i++) {
    var item = messages[i]
    if (!item) continue
    var id = String(item.id || "")
    var reference = String(item.reference_id || "")
    if (id !== "") ids[id] = true
    if (reference !== "") references[reference] = true
  }
  return { ids: ids, references: references }
}

function isMirrored(item, key, index) {
  return item.key === key && (!!index.ids[String(item.message_id || "")]
    || !!index.references[String(item.local_id || "")])
}

function prune(items, key, messages, now) {
  var index = mirrorIndex(messages)
  return items.filter(function(item) {
    if (isMirrored(item, key, index)) return false
    // File uploads are not queued; the first post-upload refresh replaces them.
    if (item.kind === "files" && item.state === "sent") return false
    if (item.state === "sent" && now - item.created > 90000) return false
    if (["failed", "queued", "waiting", "sending"].indexOf(item.state) >= 0) return true
    return now - item.created < 300000
  })
}
