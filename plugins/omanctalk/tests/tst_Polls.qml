import QtQuick
import QtTest
import "../PollModel.js" as PollModel

TestCase {
  name: "TalkPolls"

  function syntheticPoll() {
    return { question: "Synthetic?", selectable: 2, closed: false,
      options: [{ text: "Alpha", mine: true }, { text: "Beta", mine: false }] }
  }

  function test_unicodeLimits() {
    var draft = PollModel.compose("😀".repeat(255), ["😀".repeat(100), "Beta"], 2, true)
    compare(draft.error, "")
    compare(draft.multi, 2)
    compare(draft.hidden, true)
    verify(PollModel.compose("x".repeat(256), ["Alpha", "Beta"], 1).error !== "")
    verify(PollModel.compose("Synthetic?", ["x".repeat(101), "Beta"], 1).error !== "")
  }

  function test_qtSequences() {
    var sequence = { 0: " Alpha ", 1: "Beta", length: 2 }
    var draft = PollModel.compose(" Synthetic? ", sequence, 2)
    compare(draft.error, "")
    compare(draft.options, ["Alpha", "Beta"])
  }

  function test_multiVoteAndWithdrawal() {
    var poll = syntheticPoll()
    compare(PollModel.nextSelection(poll, "Beta"), ["Alpha", "Beta"])
    compare(PollModel.nextSelection(poll, "Alpha"), [])
    poll.options[1].mine = true
    compare(PollModel.nextSelection(poll, "Alpha"), ["Beta"])
  }

  function test_singleVoteReplacement() {
    var poll = syntheticPoll()
    poll.selectable = 1
    compare(PollModel.nextSelection(poll, "Beta"), ["Beta"])
    compare(PollModel.nextSelection(poll, "Alpha"), [])
  }

  function test_closedPoll() {
    var poll = syntheticPoll()
    poll.closed = true
    compare(PollModel.nextSelection(poll, "Alpha"), null)
    compare(PollModel.canClose({ poll: poll, from_me: true }, true), false)
  }

  function test_permissionAndScope() {
    var poll = syntheticPoll()
    compare(PollModel.canClose({ poll: poll, from_me: false }, false), false)
    compare(PollModel.canClose({ poll: poll, from_me: true }, false), true)
    compare(PollModel.canClose({ poll: poll, from_me: false }, true), true)
    var details = { account: "demo@example.org", chat: { jid: "room" }, group: { my_role: "moderator" } }
    compare(PollModel.isModerator(details, "demo@example.org", "room"), true)
    compare(PollModel.isModerator(details, "other@example.org", "room"), false)
    compare(PollModel.isModerator(details, "demo@example.org", "other"), false)
  }

  function test_hiddenResultsStillAllowOwnSelection() {
    var poll = syntheticPoll()
    poll.hidden_results = true
    compare(PollModel.nextSelection(poll, "Beta"), ["Alpha", "Beta"])
  }

  function test_errorFallback() {
    compare(PollModel.writeError({ error: "Synthetic failure" }, "Fallback"), "Synthetic failure")
    verify(PollModel.writeError({ supported: false }, "Fallback").indexOf("not supported") >= 0)
    compare(PollModel.writeError(null, "Fallback"), "Fallback")
  }
}
