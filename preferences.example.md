# Webex Agent Preferences

These rules train the triage system on what's relevant to you. The triage tool reads this file to determine what to scan, what to skip, and how to judge relevance.

## My Role & Focus
<!-- Who are you and what do you care about? This context is passed to the AI so it can judge relevance. -->
- Your Name (you@example.com)
- Your role and team (e.g., "Product Manager, Platform Team")
- Key areas: list your 3-5 focus areas here
- What you DON'T need: describe noise you want filtered out (e.g., "I don't need routine deploy notifications")

## Always Scan
<!-- Spaces to always check for new activity (not just @mentions). -->
<!-- Group by priority so the triage knows what matters most. -->

### Priority 1 — My Direct Programs
<!-- Spaces where you're the DRI or primary owner. Everything gets triaged. -->
- my-project-space
- cross-functional-leads

### Priority 2 — Cross-functional / Stakeholder
<!-- Spaces where decisions affect you but you're not the DRI. -->
- product-sync
- leadership-updates
- partner-team-space

### Priority 3 — Engineering / Awareness
<!-- Spaces you monitor for awareness. Lower priority in output. -->
- eng-team-chat
- metrics-and-data
- design-reviews

## Mentions Only
<!-- Only include these spaces if you're @mentioned or your name appears in text. -->
<!-- Good for large noisy channels where you only care about directed messages. -->
- help-channel
- all-hands-qa
- general-engineering
- incident-alerts

## Never Scan
<!-- Skip entirely — no API calls, no triage. Social, off-topic, personal. -->
- social-channel
- off-topic-watercooler
- fitness-groups
- buy-sell-trade

## Space-Specific Rules
<!-- Override default behavior for specific spaces. Write in natural language. -->
<!-- The AI reads these as instructions for how to handle each space. -->

### help-channel
- Only flag items where I'm directly mentioned or where the topic involves my area
- Ignore general support threads I'm not tagged in

### eng-team-chat
- Flag decisions, architecture changes, or blockers
- Ignore PR reviews, deploy notifications, and "good morning" messages

### incident-alerts
- Only flag if the incident involves my services or if I'm paged
- Ignore routine automated alerts

## Noise Patterns to Ignore
<!-- Common message patterns that are never actionable for you. -->
<!-- These apply globally across all scanned spaces. -->
- Bot messages from: deploy-bot, ci-bot, reminder-bot
- Messages that are only emoji reactions with no text
- "Good morning" / greetings with no other content
- PR merge notifications unless they mention my name
- Automated test/build failure alerts (unless I'm the code owner)
- Meeting recording share notifications
