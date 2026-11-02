---
name: roblox-monetization
description: Add or audit Robux monetization - Game Passes, Developer Products and receipt handling, subscriptions, private servers, rewarded ads, regional and personalized pricing, paid-random-item policy, and purchase testing. Load for anything a player pays for or any shop change.
---

# Roblox monetization

## Grant only on the server
- The client may prompt a purchase; value is granted only by server-owned code.
- Developer Products (repeatable): a receipt handler (`MarketplaceService.ProcessReceipt`, or `BindReceiptHandler` for developer products where the project uses it). `PromptProductPurchaseFinished` is not a purchase confirmation.
- The handler is idempotent and durable: record the `PurchaseId`, grant, save, and return `PurchaseGranted` only after the save succeeded; otherwise `NotProcessedYet` (Roblox retries). Handle unknown products and players who left.
- Game Passes: one server registry of pass id -> benefit. Apply benefits through the same idempotent path on join (`UserOwnsGamePassAsync`, wrapped in `pcall`) and on `PromptGamePassPurchaseFinished`.
- Prices and product ids live in server config, never in client messages.

## Policy
- Check `PolicyService:GetPolicyInfoForPlayerAsync` for eligibility-sensitive purchases (for example, paid random items). Where they are restricted, offer the documented alternatives (earnable, a known non-random sequence, direct purchase) rather than silently hiding the feature.
- Prices in Robux, new passes, products and premium benefits are owner decisions: propose them, do not invent them.

## Design for value, not pressure
Clear value, no pay-to-win against other players, no deceptive timers or fake scarcity; respect kid-safe expectations of the audience. Pair every paid shortcut with an earnable path. (Design depth: `roblox-game-design`.)

## Test
Duplicate receipts, a failed save during a grant, a player leaving mid-purchase, a server shutdown, restoring passes on rejoin, and a forged client request that must grant nothing.

## References
- `references/monetization.md` - passes, products, subscriptions, private servers, policy, failure handling (roblox-brain).
- `references/monetization-quick.md`, `monetization-ads-and-extras.md` - ProcessReceipt template, subscriptions, rewarded ads, regional pricing (EL4CTEO).
