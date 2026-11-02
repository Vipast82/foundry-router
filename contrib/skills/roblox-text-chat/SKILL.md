---
name: roblox-text-chat
description: Chat and player text safety - TextChatService channels, commands and bubbles, custom chat UI, mandatory filtering of any player-authored text shown to others (names, signs, notes), and PolicyService restrictions. Load for chat features or any text one player writes and another sees.
---

# Chat and text safety

## Non-negotiable (Roblox Community Standards)
- Any text one player writes and another player sees - pet names, guild names, signs, trade notes, custom titles - is filtered with `TextService:FilterStringAsync` on the server, then shown with `GetNonChatStringForBroadcastAsync` (everyone) or `GetNonChatStringForUserAsync` (one viewer). Filter again when it is shown later or to new viewers.
- Filtering yields and can fail: `pcall` it; on failure show nothing or a placeholder, never the raw text.
- Never build your own word filter as a substitute, and never send unfiltered text over a remote to other clients.
- Respect `PolicyService` results per player (chat and social restrictions); do not offer features the policy disables.

## TextChatService
- Use `TextChatService` (legacy chat is retired). Channels (`TextChannel`), `SendAsync` from the client, `ShouldDeliverCallback` on the server for who receives what, `OnIncomingMessage` to decorate (prefixes, colours) without changing content.
- Custom commands with `TextChatCommand` (aliases, server-side handling and permission checks).
- Bubble chat and the chat window are configured through `BubbleChatConfiguration` and `ChatWindowConfiguration`.
- System messages for game events go through a channel's `DisplaySystemMessage`; localize them.

## Names
Display names are not unique; identify players by `UserId` (or `player.User`). Show display names in UI, use ids in logic and saves.

## References
- `references/text-chat.md`, `text-chat-chat-recipes.md` - filtering rules, TextChatService essentials, PolicyService, recipes (EL4CTEO).
