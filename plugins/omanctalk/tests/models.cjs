// Offline, synthetic model and UI-contract checks (no helper processes).
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { spawnSync } = require('node:child_process');
const root = path.resolve(__dirname, '..');
const source = name => fs.readFileSync(path.join(root, name), 'utf8');
function model(name) {
  const context = vm.createContext({});
  vm.runInContext(source(name).replace(/^\.(pragma|import).*$/gm, ''), context, { filename: name });
  return context;
}
const format = model('FormatModel.js');
const account = model('AccountModel.js');
const composer = model('ComposerModel.js');
const pending = model('PendingModel.js');
const media = model('MediaModel.js');
const polls = model('PollModel.js');

for (const [label, text, expected] of [
  ['bold', '**hello**', '<b>hello</b>'],
  ['italic', '*hello*', '<i>hello</i>'],
  ['strike', '~~hello~~', '<s>hello</s>'],
  ['code', '`<b>*hello*</b>`', '<code>&lt;b&gt;*hello*&lt;/b&gt;</code>'],
  ['list', '- hello\n1. world', '• hello<br>1. world'],
  ['quote', '> hello', '▍ hello'],
  ['mention', 'Hi @Demo User!', '<b>@Demo User</b>!'],
  ['markdown link', '[cloud](https://example.org)', '<a href="https://example.org">cloud</a>'],
  ['mailto link', '[mail](mailto:demo@example.org)', '<a href="mailto:demo@example.org">mail</a>'],
  ['bare link', 'https://example.org.', '<a href="https://example.org">https://example.org</a>.'],
]) test('Markdown: ' + label, () => assert.ok(format.toHtml(text).includes(expected)));

test('HTML is escaped, never rendered', () => {
  const html = format.toHtml('<img src=x onerror="bad()"><script>bad()</script>');
  assert.ok(html.includes('&lt;img'));
  assert.ok(!html.includes('<img') && !html.includes('<script'));
});
test('fenced code is escaped without inline formatting', () => {
  const html = format.toHtml('```js\n<img> **plain**\n```');
  assert.ok(html.includes('&lt;img&gt; **plain**'));
  assert.ok(!html.includes('<b>'));
});
test('unclosed fenced code is still visible and safe', () => assert.ok(format.toHtml('```\n<demo>').includes('&lt;demo&gt;')));
test('unsafe links never become anchors', () => {
  for (const url of ['javascript:bad()', 'file:///tmp/demo', 'data:text/html,demo', 'https://example.org/"bad', 'https://example.org/\n']) {
    assert.equal(format.safeLink(url), false);
    const html = format.toHtml('[demo](' + url + ')');
    if (!url.includes('\n')) assert.ok(!html.includes('<a href='));
    for (const match of html.matchAll(/href="([^"]+)"/g)) assert.equal(format.safeLink(match[1]), true);
  }
});
test('palette values cannot inject HTML attributes', () => {
  const html = format.toHtml('> demo\n```\nx\n```', { dim: 'red" onclick="bad', code: 'bad"' });
  assert.ok(!html.includes('onclick') && !html.includes('bad"'));
});
test('email addresses are not mentions', () => assert.ok(!format.toHtml('demo@example.org').includes('<b>')));
test('composer bold is Markdown and toggles', () => {
  const wrapped = format.wrap('hello', 0, 5, 'bold');
  assert.equal(wrapped.text, '**hello**');
  assert.equal(format.wrap(wrapped.text, wrapped.start, wrapped.end, 'bold').text, 'hello');
});
test('composer italic and strike are Markdown', () => {
  assert.equal(format.wrap('x', 0, 1, 'italic').text, '*x*');
  assert.equal(format.wrap('x', 0, 1, 'strike').text, '~~x~~');
});
test('composer code blocks render their selection', () => {
  const result = format.wrap('<demo>', 0, 6, 'mono');
  assert.equal(result.text, '```\n<demo>\n```');
  assert.ok(format.toHtml(result.text).includes('&lt;demo&gt;'));
});
test('line prefixes toggle without losing text', () => {
  const result = format.prefixLines('one\ntwo', 0, 7, 'numbered');
  assert.equal(result.text, '1. one\n2. two');
  assert.equal(format.prefixLines(result.text, 0, result.text.length, 'numbered').text, 'one\ntwo');
});
test('minimal editor edit reconstructs Markdown', () => {
  const result = format.apply('hello world', 6, 11, 'bold');
  assert.equal('hello world'.slice(0, result.head) + result.insert + 'hello world'.slice(result.end), 'hello **world**');
});
test('plain previews strip Markdown markers', () => assert.equal(format.plain('**hello** *world* ~~x~~ `code`'), 'hello world x code'));
test('signatures use Markdown and are idempotent', () => {
  for (const position of ['top', 'bottom']) {
    const signature = { enabled: true, name: 'Demo', position };
    const text = composer.signedText('synthetic', signature);
    assert.ok(text.includes('**Demo'));
    assert.equal(composer.signedText(text, signature), text);
  }
});
test('identical room tokens remain account scoped', () => {
  assert.equal(account.sameRef(account.chatRef('demo@one', 'token'), account.chatRef('demo@two', 'token')), false);
  assert.equal(account.sameRef(account.chatRef('demo@one', 'token'), account.chatRef('demo@one', 'token')), true);
});
test('late responses cannot land in another account', () => assert.equal(account.responseMatches({ jid: 'token' }, account.chatRef('demo@one', 'token'), account.chatRef('demo@two', 'token')), false));
test('store roots belong to OmaNCTalk', () => assert.equal(account.defaultStoreDirectory('', '', '/home/demo'), '/home/demo/.local/state/omanctalk/stores'));
test('offline rail readiness does not imply authenticated writes', () => {
  const result = account.statusReadiness({ authenticated: false, database_ready: true, rail_ready: true });
  assert.equal(result.accountReady, false);
  assert.equal(result.railReady, true);
});
test('system messages never join a sender run', () => assert.equal(account.sameRun({ system: true, from_me: false }, { from_me: false }), false));
test('forward targets stay in the origin account', () => {
  const chats = [{ account: 'one', jid: 'a' }, { account: 'one', jid: 'b' }, { account: 'two', jid: 'b' }];
  assert.equal(account.forwardTargets(chats, chats[0]).length, 1);
});

// Evaluate existing QML JavaScript functions against inert process objects.
function qmlFunction(name, file = 'AccountOperations.qml') {
  const text = source(file);
  const start = text.indexOf('function ' + name + '(');
  assert.ok(start >= 0);
  const open = text.indexOf('{', start);
  let depth = 1, end = open + 1;
  while (depth && end < text.length) {
    if (text[end] === '{') depth++;
    if (text[end] === '}') depth--;
    end++;
  }
  return text.slice(start, end);
}
function loginHarness() {
  const timer = () => ({ interval: 0, starts: 0, restart() { this.starts++; }, stop() {} });
  const state = { generation: 0, linkPhase: 'idle', statusMessage: '', deadline: 0 };
  Object.defineProperty(state, 'linkBusy', { get() { return this.linkPhase !== 'idle'; } });
  Object.defineProperty(state, 'busy', { get() { return this.linkBusy; } });
  const context = vm.createContext({ root: state, startProcess: { running: false }, finishProcess: { running: false }, pollTimer: timer(), watchdog: timer() });
  for (const name of ['linkAccount', 'cancelLogin', 'pollLogin']) vm.runInContext(qmlFunction(name), context);
  // QML resolves unqualified property names on the owning Item.
  for (const key of ['generation', 'busy', 'linkBusy', 'linkPhase', 'linkTarget', 'deadline', 'pollToken', 'pollEndpoint', 'statusMessage'])
    Object.defineProperty(context, key, { get() { return state[key]; }, set(value) { state[key] = value; } });
  for (const name of ['cancelLogin']) state[name] = context[name];
  return { context, state };
}
test('login rejects unsafe server schemes', () => {
  const { context } = loginHarness();
  for (const server of ['http://example.org', 'file:///tmp/demo', 'https://example.org/?token=demo']) assert.equal(context.linkAccount(server), false);
});
test('login starts once and passes server through stdin payload', () => {
  const { context, state } = loginHarness();
  assert.equal(context.linkAccount('https://example.org/cloud/'), true);
  assert.equal(JSON.parse(context.startProcess.payload).server, 'https://example.org/cloud');
  assert.equal(state.linkPhase, 'starting');
  assert.equal(context.linkAccount('https://example.org'), false);
  assert.equal(context.watchdog.starts, 1);
});
test('login polling is single-flight', () => {
  const { context, state } = loginHarness();
  state.linkPhase = 'waiting'; state.deadline = Date.now() + 10000;
  state.linkTarget = 'https://example.org'; state.pollToken = 'synthetic-poll'; state.pollEndpoint = 'https://example.org/poll';
  context.pollLogin();
  assert.equal(JSON.parse(context.finishProcess.payload).poll_token, 'synthetic-poll');
  const before = context.finishProcess.payload;
  context.pollLogin();
  assert.equal(context.finishProcess.payload, before);
});
test('login cancellation clears secrets and invalidates callbacks', () => {
  const { context, state } = loginHarness();
  state.linkPhase = 'waiting'; state.pollToken = 'synthetic-poll'; state.pollEndpoint = 'https://example.org/poll';
  context.cancelLogin();
  assert.equal(state.linkPhase, 'idle'); assert.equal(state.pollToken, ''); assert.equal(state.pollEndpoint, ''); assert.equal(state.generation, 1);
});
test('expired login is stopped instead of polling forever', () => {
  const { context, state } = loginHarness();
  state.linkPhase = 'waiting'; state.deadline = 1;
  context.pollLogin();
  assert.equal(state.linkPhase, 'idle'); assert.equal(context.finishProcess.running, false);
});
test('login interval, timeout and stale-callback guards exist', () => {
  const text = source('AccountOperations.qml');
  assert.ok(text.includes('interval: 2000'));
  assert.ok(text.includes('20 * 60 * 1000'));
  assert.equal((text.match(/generation !== root.generation/g) || []).length, 2);
  assert.ok(text.includes('response.pending === true'));
});
test('Talk watcher, identity and delete transport match the contract', () => {
  const text = source('Service.qml');
  assert.ok(text.includes('name === "talk.db" || name === "talk.db-wal"'));
  assert.ok(text.includes('target: { user: target.substring(6) }'));
  assert.ok(text.includes('runControl("account-remove"'));
  assert.ok(!text.includes('for_me:'));
  assert.ok(text.includes('readonly property bool presenceSupported: false'));
});
test('unsupported composer features are visibly gated', () => {
  const app = source('App.qml'), dropdown = source('Dropdown.qml');
  assert.ok(app.includes('stickersEnabled: false') && dropdown.includes('stickersEnabled: false'));
  assert.ok(app.includes('label: "Poll"') && !app.includes('label: "New sticker"'));
  assert.ok(!source('SettingsView.qml').includes('key: "show_online"'));
  assert.ok(!fs.existsSync(path.join(root, 'UpdateController.qml')));
  assert.ok(!fs.existsSync(path.join(root, 'MaintenanceSettings.qml')));
});
test('synthetic UI sources contain no old brand or device setup', () => {
  for (const name of fs.readdirSync(root).filter(name => /\.(qml|js)$/.test(name)))
    assert.ok(!/whatsapp|wacli|\bQR\b/i.test(source(name)), name);
});
test('local IDs separate sessions even at the same start time', () => {
  const context = vm.createContext({ Date: { now: () => 1700000000000 } });
  vm.runInContext(source('PendingModel.js').replace(/^\.pragma.*$/gm, ''), context);
  const sessions = Array.from({ length: 100 }, () => context.sessionPrefix());
  assert.equal(new Set(sessions).size, sessions.length);
  const ids = sessions.flatMap(session => [1, 2, Number.MAX_SAFE_INTEGER].map(serial => context.localId(session, serial)));
  assert.equal(new Set(ids).size, ids.length);
  for (const id of ids) assert.match(id, /^[A-Za-z0-9._-]{1,64}$/);
});
test('all optimistic send paths use the session ID generator', () => {
  const text = source('Service.qml');
  assert.equal((text.match(/var localId = nextPendingId\(\)/g) || []).length, 3);
  assert.ok(text.includes('PendingModel.sessionPrefix()'));
  assert.ok(text.includes('PendingModel.localId(pendingSendSession, pendingSendSerial)'));
  assert.ok(!text.includes('"pending:"'));
});
test('queued send answers are not marked sent', () => {
  const result = pending.sendResult({ queued: true }, 123);
  assert.equal(result.state, 'queued'); assert.equal(result.message_id, ''); assert.equal(result.created, 123);
  assert.ok(source('Service.qml').includes('PendingModel.sendResult(payload, Date.now())'));
  assert.ok(source('MessageBubble.qml').includes('Waiting for connection'));
});
test('online send answers retain message ID tracking', () => {
  const result = pending.sendResult({ queued: false, message_id: '42' }, 123);
  assert.equal(result.state, 'sent'); assert.equal(result.message_id, '42');
  assert.equal(pending.sendResult({ message_id: '43' }, 123).state, 'sent');
});
const chatKey = 'demo@example.org\nroom';
const pendingItem = changes => Object.assign({ key: chatKey, local_id: 'synthetic-session-1', message_id: '', state: 'queued', created: 1000 }, changes);
test('queued bubbles survive both old expiration limits', () => {
  const item = pendingItem();
  for (const now of [91001, 301001, 86401000]) assert.equal(pending.prune([item], chatKey, [], now).length, 1);
});
test('unsent states stay visible until resolved', () => {
  for (const state of ['waiting', 'sending', 'failed'])
    assert.equal(pending.prune([pendingItem({ state })], chatKey, [], 86401000).length, 1);
});
test('reference_id removes a queued bubble without a message ID', () => {
  const item = pendingItem();
  const messages = [{ id: '42', reference_id: item.local_id }];
  assert.equal(pending.prune([item], chatKey, messages, 86401000).length, 0);
  assert.equal(pending.isMirrored(item, chatKey, pending.mirrorIndex(messages)), true);
  assert.ok(source('Service.qml').includes('!PendingModel.isMirrored(item, key, mirror)'));
});
test('mirror reconciliation is idempotent and only removes matching sends', () => {
  const items = [pendingItem(), pendingItem({ local_id: 'synthetic-session-2' })];
  const messages = [{ id: '42', reference_id: items[0].local_id }];
  const result = pending.prune(items, chatKey, messages, 1000);
  assert.equal(result.length, 1); assert.equal(result[0].local_id, items[1].local_id);
  assert.equal(pending.prune(result, chatKey, messages, 1000).length, 1);
  assert.equal(items.length, 2);
});
test('reference reconciliation works before the helper answer', () => {
  assert.equal(pending.prune([pendingItem({ state: 'sending' })], chatKey,
    [{ id: '42', reference_id: 'synthetic-session-1' }], 1000).length, 0);
});
test('unrelated messages and empty references never match', () => {
  const item = pendingItem();
  for (const messages of [[{ id: '42' }], [{ id: '42', reference_id: '' }], [{ id: '42', reference_id: 'other' }]])
    assert.equal(pending.prune([item], chatKey, messages, 1000).length, 1);
});
test('pending reconciliation never crosses account or room boundaries', () => {
  const messages = [{ id: '42', reference_id: 'synthetic-session-1' }];
  for (const key of ['other@example.org\nroom', 'demo@example.org\nother'])
    assert.equal(pending.prune([pendingItem()], key, messages, 1000).length, 1);
});
test('sent bubbles still reconcile by message_id and expire after 90 seconds', () => {
  const item = pendingItem({ state: 'sent', message_id: '42' });
  assert.equal(pending.prune([item], chatKey, [{ id: '42' }], 1000).length, 0);
  assert.equal(pending.prune([item], chatKey, [], 91000).length, 1);
  assert.equal(pending.prune([item], chatKey, [], 91001).length, 0);
});
test('file bubble cleanup and prototype-like IDs are safe', () => {
  assert.equal(pending.prune([pendingItem({ kind: 'files', state: 'sent' })], chatKey, [], 1000).length, 0);
  assert.equal(pending.isMirrored(pendingItem({ local_id: 'constructor', message_id: 'toString' }), chatKey, pending.mirrorIndex([])), false);
});
test('service reconciliation includes loaded older mirror pages', () => {
  const text = source('Service.qml');
  assert.ok(text.includes('PendingModel.mirrorIndex(messages.concat(older))'));
  assert.ok(text.includes('messages.concat(olderMessages), Date.now())'));
});
test('voice DTO flag overrides file names', () => {
  for (const filename of ['', 'upload.ogg', 'upload.opus', 'upload.mp3']) {
    assert.equal(media.isVoiceNote({ media_type: 'audio', filename, voice: false }), false);
    assert.equal(media.isVoiceNote({ media_type: 'audio', filename, voice: true }), true);
  }
  assert.equal(media.isVoiceNote({ media_type: 'document', voice: true }), false);
  assert.ok(source('MediaBubble.qml').includes('MediaModel.isVoiceNote(message)'));
});
test('voice filename heuristics only apply to legacy DTOs', () => {
  for (const filename of ['', 'legacy.OGG', 'legacy.opus']) assert.equal(media.isVoiceNote({ media_type: 'audio', filename }), true);
  assert.equal(media.isVoiceNote({ media_type: 'audio', filename: 'legacy.mp3' }), false);
  assert.equal(media.isVoiceNote({ media_type: 'audio', filename: '', voice: null }), false);
});
test('peer avatars are account-scoped and retain the legacy room key', () => {
  const map = account.avatarIndex([
    { account: 'one', jid: 'room', peer_jid: 'users/demo', avatar_path: '/synthetic/one.png', kind: 'dm' },
    { account: 'two', jid: 'room', peer_jid: 'users/demo', avatar_path: '/synthetic/two.png', kind: 'dm' },
    { account: 'one', jid: 'legacy', avatar_path: '/synthetic/legacy.png', kind: 'dm' },
    { account: 'one', jid: 'group', peer_jid: 'users/group', avatar_path: '/synthetic/group.png', kind: 'group' }
  ]);
  assert.equal(map['one\nusers/demo'], '/synthetic/one.png');
  assert.equal(map['two\nusers/demo'], '/synthetic/two.png');
  assert.equal(map['one\nroom'], '/synthetic/one.png');
  assert.equal(map['one\nlegacy'], '/synthetic/legacy.png');
  assert.equal(map['one\nusers/group'], undefined);
  assert.equal(map['one\n'], undefined);
  assert.ok(source('App.qml').includes('AccountModel.avatarIndex(root.sourceChats || [])'));
});
test('guest, bot and federated actors have no moderator actions', () => {
  const context = vm.createContext({ admin: true, menuArmed: '' });
  vm.runInContext(qmlFunction('participantActions', 'ChatDetailsPanel.qml'), context);
  for (const jid of ['guests/demo', 'bots/demo', 'federated_users/demo', ''])
    assert.deepEqual(Array.from(context.participantActions({ jid, role: 'guest' }), item => item.key), ['message']);
  assert.deepEqual(Array.from(context.participantActions({ jid: 'users/demo', role: 'user' }), item => item.key), ['message', 'promote', 'remove']);
  assert.deepEqual(Array.from(context.participantActions({ jid: 'users/demo', role: 'moderator' }), item => item.key), ['message', 'demote', 'remove']);
  for (const person of [{ jid: 'users/demo', me: true }, { jid: 'users/demo', role: 'owner' }])
    assert.equal(context.participantActions(person).length, 1);
  context.admin = false;
  assert.equal(context.participantActions({ jid: 'users/demo' }).length, 1);
});
test('account removal sends account and requires matching confirmation', () => {
  const calls = [];
  const context = vm.createContext({ controlProcess: { running: false }, writing: false,
    runControl(kind, payload) { calls.push({ kind, payload }); return true; } });
  vm.runInContext(qmlFunction('unlinkAccount', 'Service.qml'), context);
  for (const [identity, confirm] of [['', ''], ['demo@example.org', ''], ['demo@example.org', 'other']])
    assert.equal(context.unlinkAccount(identity, confirm), false);
  assert.equal(context.unlinkAccount('demo@example.org', 'demo@example.org'), true);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].kind, 'account-remove');
  assert.equal(calls[0].payload.account, 'demo@example.org');
  assert.equal(calls[0].payload.confirm, 'demo@example.org');
  assert.equal(calls[0].payload.name, undefined);
  assert.ok(source('SettingsView.qml').includes('service.unlinkAccount(name, name)'));
  for (const file of ['App.qml', 'Dropdown.qml']) assert.ok(source(file).includes('service.unlinkAccount(account, confirm)'));
  assert.ok(source('AccountSwitcher.qml').includes('root.removeRequested(root.removalAccount, root.removalAccount)'));
});
test('media settings describe daemon policy without ineffective avatar actions', () => {
  assert.ok(source('SettingsView.qml').includes('Images up to 20 MiB'));
  for (const file of ['SettingsView.qml', 'Service.qml', 'AccountOperations.qml']) {
    const text = source(file);
    assert.ok(!text.includes('refreshAvatars'));
    assert.ok(!text.includes('refresh_avatars'));
    assert.ok(!text.includes('avatarProcess'));
    assert.ok(!text.includes('autoAvatarTimer'));
  }
});
const syntheticPoll = changes => Object.assign({ question: 'Synthetic question?', selectable: 1,
  options: [{ text: 'Alpha', votes: 2, voters: ['Demo'], mine: false },
    { text: 'Beta', votes: 0, voters: [], mine: false }], closed: false }, changes);
const plain = value => JSON.parse(JSON.stringify(value));
test('poll composer trims options and sends the selection count, not a boolean', () => {
  assert.deepEqual(plain(polls.compose(' Synthetic? ', [' Alpha ', '', 'Beta'], 2, true)),
    { error: '', question: 'Synthetic?', options: ['Alpha', 'Beta'], multi: 2, hidden: true });
  assert.equal(polls.compose('Synthetic?', ['Alpha', 'Beta'], 1).hidden, false);
});
test('poll question and option limits include Unicode code points', () => {
  assert.equal(polls.compose('😀'.repeat(255), ['😀'.repeat(100), 'Beta'], 1).error, '');
  assert.match(polls.compose('x'.repeat(256), ['Alpha', 'Beta'], 1).error, /255/);
  assert.match(polls.compose('Synthetic?', ['x'.repeat(101), 'Beta'], 1).error, /100/);
  assert.match(polls.compose(' ', ['Alpha', 'Beta'], 1).error, /question/);
});
test('poll option counts, duplicates and invalid selection limits are rejected', () => {
  for (const size of [0, 1, 13])
    assert.match(polls.compose('Synthetic?', Array.from({ length: size }, (_, i) => 'Option ' + i), 1).error, /2 and 12/);
  for (const size of [2, 12])
    assert.equal(polls.compose('Synthetic?', Array.from({ length: size }, (_, i) => 'Option ' + i), size).error, '');
  assert.match(polls.compose('Synthetic?', ['Alpha', 'alpha'], 1).error, /unique/);
  for (const multi of [0, -1, 3, 1.5, NaN])
    assert.match(polls.compose('Synthetic?', ['Alpha', 'Beta'], multi).error, /limit/);
});
test('poll lists reject malformed lengths without looping or clearing votes', () => {
  for (const length of [Infinity, NaN, -1, 1.5, 1000000])
    assert.deepEqual(plain(polls.listOf({ length })), []);
  const { context, ref, process } = pollServiceHarness();
  for (const options of [{}, { length: Infinity }, { length: -1 }, 'Alpha'])
    assert.equal(context.votePoll(ref, { id: '42', poll: syntheticPoll() }, options, 'app'), false);
  assert.equal(process.running, false);
});
test('single-choice tap votes, replaces or withdraws a vote', () => {
  const poll = syntheticPoll();
  assert.deepEqual(plain(polls.nextSelection(poll, 'Alpha')), ['Alpha']);
  poll.options[0].mine = true;
  assert.deepEqual(plain(polls.nextSelection(poll, 'Beta')), ['Beta']);
  assert.deepEqual(plain(polls.nextSelection(poll, 'Alpha')), []);
  assert.equal(polls.nextSelection(poll, 'Unknown'), null);
});
test('multiple-choice tap adds, removes and permits empty selection', () => {
  const poll = syntheticPoll({ selectable: 2 });
  poll.options[0].mine = true;
  assert.deepEqual(plain(polls.nextSelection(poll, 'Beta')), ['Alpha', 'Beta']);
  assert.deepEqual(plain(polls.nextSelection(poll, 'Alpha')), []);
  poll.options[1].mine = true;
  assert.deepEqual(plain(polls.nextSelection(poll, 'Beta')), ['Alpha']);
  poll.options.push({ text: 'Gamma', mine: false });
  assert.equal(polls.nextSelection(poll, 'Gamma'), null);
});
test('closed polls never permit voting, even if an option is currently selected', () => {
  const poll = syntheticPoll({ closed: true });
  poll.options[0].mine = true;
  assert.equal(polls.nextSelection(poll, 'Alpha'), null);
});
test('poll closing is limited to open polls by creator or known moderator', () => {
  const item = { id: '42', from_me: true, poll: syntheticPoll() };
  assert.equal(polls.canClose(item, false), true);
  item.from_me = false;
  assert.equal(polls.canClose(item, false), false);
  assert.equal(polls.canClose(item, true), true);
  for (const flag of ['pending', 'revoked', 'system'])
    assert.equal(polls.canClose({ ...item, [flag]: true }, true), false);
  item.poll.closed = true;
  assert.equal(polls.canClose(item, true), false);
});
test('moderator rights are scoped to the exact account and room', () => {
  const details = { account: 'demo@example.org', chat: { jid: 'room' }, group: { my_role: 'moderator' } };
  assert.equal(polls.isModerator(details, 'demo@example.org', 'room'), true);
  assert.equal(polls.isModerator(details, 'other@example.org', 'room'), false);
  assert.equal(polls.isModerator(details, 'demo@example.org', 'other'), false);
  for (const role of ['guest', 'user', '']) {
    details.group.my_role = role;
    assert.equal(polls.isModerator(details, 'demo@example.org', 'room'), false);
  }
});
function pollServiceHarness() {
  const failures = [], process = { running: false };
  const ref = account.chatRef('demo@example.org', 'room');
  const context = vm.createContext({ PollModel: polls, AccountModel: account, helper: '/synthetic/omanctalk',
    writing: false, ready: true, statusReady: true, statusAccount: ref.account, offlineMode: false,
    chatDetails: {}, writeProcess: process, writeFailed(...args) { failures.push(args); }, refreshStatus() {} });
  for (const name of ['writeOwner', 'writeRefusal', 'runWriteForChat', 'sendPoll', 'canModeratePoll', 'votePoll', 'closePoll'])
    vm.runInContext(qmlFunction(name, 'Service.qml'), context);
  return { context, ref, process, failures };
}
test('poll service transport passes account, room, hidden and multi through stdin', () => {
  const { context, ref, process } = pollServiceHarness();
  assert.equal(context.sendPoll(ref, 'Synthetic?', ['Alpha', 'Beta'], 2, 'app', true), true);
  assert.deepEqual(plain(process.command), ['/synthetic/omanctalk', 'poll']);
  assert.deepEqual(JSON.parse(process.payload), { question: 'Synthetic?', options: ['Alpha', 'Beta'], multi: 2,
    hidden: true, account: ref.account, jid: ref.jid });
  assert.equal(context.sendPoll(ref, 'Synthetic?', ['Alpha', 'Beta'], 1, 'app'), false);
});
test('poll vote sends empty lists and uses the message ID, not the poll ID', () => {
  const { context, ref, process } = pollServiceHarness();
  assert.equal(context.votePoll(ref, { id: '42', poll: syntheticPoll({ id: '99' }) }, [], 'dropdown'), true);
  assert.equal(process.command[1], 'poll-vote');
  assert.deepEqual(JSON.parse(process.payload), { id: '42', options: [], account: ref.account, jid: ref.jid });
});
test('poll close transport uses the message ID and checks permission first', () => {
  const { context, ref, process } = pollServiceHarness();
  const item = { id: '42', from_me: false, poll: syntheticPoll({ id: '99' }) };
  assert.equal(context.closePoll(ref, item, 'app'), false);
  item.from_me = true;
  assert.equal(context.closePoll(ref, item, 'app'), true);
  assert.equal(process.command[1], 'poll-close');
  assert.deepEqual(JSON.parse(process.payload), { id: '42', account: ref.account, jid: ref.jid });
});
test('offline, unsigned and account-switching polls cannot start helper processes', () => {
  for (const change of [{ offlineMode: true }, { ready: false }, { statusReady: false }, { statusAccount: 'other' }]) {
    const { context, ref, process, failures } = pollServiceHarness();
    Object.assign(context, change);
    assert.equal(context.sendPoll(ref, 'Synthetic?', ['Alpha', 'Beta'], 1, 'app'), false);
    assert.equal(process.running, false);
    assert.equal(failures.length, 1);
    assert.ok(failures[0][0]);
  }
});
test('invalid poll drafts show an error without a helper call', () => {
  const { context, ref, process, failures } = pollServiceHarness();
  assert.equal(context.sendPoll(ref, 'Synthetic?', ['Alpha'], 1, 'app'), false);
  assert.equal(process.running, false);
  assert.match(context.errorText, /2 and 12/);
  assert.equal(failures[0][2].kind, 'poll');
});
test('poll service errors include unsupported responses and preserve backend errors', () => {
  assert.match(polls.writeError({ supported: false }, 'Fallback'), /not supported/);
  assert.equal(polls.writeError({ error: 'Synthetic failure' }, 'Fallback'), 'Synthetic failure');
  assert.equal(polls.writeError(null, 'Fallback'), 'Fallback');
  const text = source('Service.qml');
  assert.ok(text.includes('payload.ok !== true || payload.supported === false'));
  assert.ok(text.includes('details.supported = false'));
  assert.ok(text.includes('Qt.callLater(root.refreshMessages)'));
});
function pollCompletionHarness(result, selected = account.chatRef('demo@example.org', 'room'), kind = 'poll-vote') {
  const ref = account.chatRef('demo@example.org', 'room');
  const queued = [], failures = [], completions = [];
  const refresh = () => {};
  const state = { writing: true, replyKinds: [], messages: [{ id: '42', poll: syntheticPoll() }],
    selectedChatRef: () => selected, parseJson: () => result, helperErrorText: (_, fallback) => fallback,
    runNextQueuedSend() {}, runNextBatchJob() {}, refreshMessages: refresh,
    writeFailed(...args) { failures.push(args); }, writeCompleted(...args) { completions.push(args); } };
  const timer = { starts: 0, restart() { this.starts++; } };
  const context = vm.createContext({ root: state, AccountModel: account, PollModel: polls,
    kind, chatRef: ref, request: { id: '42', options: [] }, owner: 'app',
    writeOutput: { text: '' }, writeError: { text: '' }, refreshDelay: timer,
    Qt: { callLater(fn) { queued.push(fn); } } });
  const text = source('Service.qml').split('id: writeProcess')[1];
  const callback = text.slice(text.indexOf('function(exitCode)'), text.indexOf('\n  }\n\n  Timer {'));
  const finish = vm.runInContext('(' + callback + ')', context);
  return { finish, state, queued, failures, completions, timer, refresh };
}
test('successful poll writes reload the selected card immediately without a message in the response', () => {
  for (const kind of ['poll', 'poll-vote', 'poll-close']) {
    const harness = pollCompletionHarness({ ok: true, kind }, undefined, kind);
    harness.finish(0);
    assert.equal(harness.state.writing, false);
    assert.equal(harness.queued.filter(fn => fn === harness.refresh).length, 1);
    assert.equal(harness.completions.length, 1);
    assert.equal(harness.timer.starts, 1);
    assert.equal(harness.state.messages[0].id, '42');
  }
});
test('poll completion never refreshes a different selected account', () => {
  const harness = pollCompletionHarness({ ok: true }, account.chatRef('other@example.org', 'room'));
  harness.finish(0);
  assert.equal(harness.queued.length, 0);
});
test('failed and unsupported poll replies are visible and never reported as successful', () => {
  for (const [result, exitCode] of [[{ ok: false, error: 'Synthetic failure' }, 1],
    [{ ok: false, supported: false }, 1], [{ ok: true, supported: false }, 0], [null, 1]]) {
    const harness = pollCompletionHarness(result);
    harness.finish(exitCode);
    assert.equal(harness.state.writing, false);
    assert.equal(harness.failures.length, 1);
    assert.ok(harness.state.errorText);
    assert.equal(harness.completions.length, 0);
    assert.equal(harness.queued.length, 0);
    if (result && result.supported === false) assert.equal(harness.failures[0][2].supported, false);
  }
});
test('poll cards wire voting and closing on both surfaces and conceal all results', () => {
  const bubble = source('MessageBubble.qml');
  assert.ok(bubble.includes('enabled: root.pollCanVote'));
  assert.ok(bubble.includes('PollModel.nextSelection(poll, text)'));
  assert.ok(bubble.includes('label: "Close poll", action: "poll-close", show: root.pollCanClose'));
  assert.ok(bubble.includes('text: "Poll closed"'));
  assert.ok(bubble.includes('text: "Results are hidden until the poll closes"'));
  assert.ok(bubble.includes('? "Select one or more" : "Select one"'));
  assert.equal((bubble.match(/visible: !root.pollHiddenResults/g) || []).length, 4);
  assert.ok(bubble.includes('if (root.pollHiddenResults) return ""'));
  for (const file of ['App.qml', 'Dropdown.qml']) {
    const text = source(file);
    assert.ok(text.includes('onPollCloseRequested:'));
    assert.ok(text.includes('service.closePoll(root.currentChatRef(), modelData'));
    assert.ok(text.includes('!root.service.writing && !root.service.offlineMode'));
  }
});
test('poll composer exposes hidden results and retains its validation error in the popup', () => {
  const text = source('App.qml');
  assert.ok(text.includes('objectName: "pollHiddenToggle"'));
  assert.ok(text.includes('objectName: "pollComposerError"'));
  assert.ok(text.includes('root.submitPoll(question, values, root.pollMultiple, root.pollHidden)'));
});

const qtRunner = ['/usr/lib/qt6/bin/qmltestrunner', '/usr/bin/qmltestrunner'].find(file => fs.existsSync(file));
test('poll model also passes in the Qt JavaScript engine', { skip: !qtRunner }, () => {
  const result = spawnSync(qtRunner, ['-input', path.join(__dirname, 'tst_Polls.qml')], {
    encoding: 'utf8', timeout: 30000, env: { ...process.env, QT_QPA_PLATFORM: 'offscreen' }
  });
  assert.equal(result.status, 0, result.stdout + result.stderr);
});

test('manifest identifies the Talk fork', () => {
  const manifest = JSON.parse(fs.readFileSync(path.resolve(root, '../../manifest.json'), 'utf8'));
  assert.equal(manifest.id, 'io.github.manueldell.omanctalk'); assert.equal(manifest.version, '0.1.0'); assert.equal(manifest.author, 'ManuelDell');
  assert.deepEqual(manifest.barWidget.aliases, ['talk', 'nextcloud', 'chat', 'messages']);
});
