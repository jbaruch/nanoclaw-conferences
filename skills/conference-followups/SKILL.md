---
name: conference-followups
description: >
  Follow up with people Baruch met at a conference: from his list of names and
  conversation notes (text or voice), find each person's real address, draft one
  short email each in his voice, and post each as a one-tap «Отправить» card;
  offer a calendar hold only where a meeting was actually agreed. Use for
  "follow up with people I met", "conference contacts", or a post-event list.
---

# Conference Follow-ups

Process steps in order. Do not skip ahead. Main only. Nothing is sent without Baruch's tap.

## Step 1 — Ground the list

From Baruch's message or voice transcript, take the conference (name and the date it started) and each person with what they talked about. Use only what he said: no invented people, addresses, promises or meetings. If the conference, its date or a person is unclear, ask him one question and finish here. Proceed immediately to Step 2.

## Step 2 — Find each address

- An address he gave comes first.
- Otherwise look for earlier mail with that person through `Skill(skill: "tessl__ask-my-data")`, or read the conference's Sessionize speaker profile.
- Never build an address from a name, an employer's domain or someone else's profile.
- Ask him about an ambiguous match.

People without an address stay unsent; list them at the end. With nobody left to write to, go to Step 4. Proceed immediately to Step 3.

## Step 3 — Draft and post one card per person

Draft one short plain-text email per person: the conference, what you talked about, and the follow-up they agreed to, nothing more. Run it through `Skill(skill: "tessl__blog-writer")` to make it sound like Baruch, keeping its language and facts.

Post each with `mcp__nanoclaw__send_message`. The text is exactly the host's template, with one button:

```text
✉️ Письмо для <address>
Тема: <subject>

<body>
```

`buttons: [[{"label": "Отправить", "action": "gmail.send.v1", "payload": {"schema_version": 1, "to": "<address>", "subject": "<subject>", "body": "<body>"}}]]`

The host refuses a card whose text differs from the payload, more than one recipient, or a display name; fix what it names and post again. The host sends on his tap, whenever it comes, and edits the card with the result. To change a draft, post a new card. Proceed immediately to Step 4.

## Step 4 — Offer calendar holds

Only for a meeting the list actually agreed, with a known time and duration: ask Baruch with one `owner.answer.v1` card whether to hold it («Поставить» / «Не надо»). On «Поставить», follow `## Requested calendar event` in the admin plugin's `skills/heartbeat/references/google-main-writes.md`. No agreed meeting, no hold. Finish here.
