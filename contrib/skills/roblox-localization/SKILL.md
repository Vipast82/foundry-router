---
name: roblox-localization
description: Add or fix translation and locale-aware content - LocalizationTable, auto-translation and AutoLocalize, Translator for manual strings and parameters, locale and country detection, and testing locales. Load for any player-facing text that must be translatable or region-specific content.
---

# Roblox localization

- Every player-facing string has a key in a `LocalizationTable` (`SourceText`, `SourceLocaleId`, one column per locale). Keep source strings in one place (a strings module or table) rather than scattered literals.
- Auto-translation needs `AutoLocalize = true` on the GUI object and its ancestors plus a matching entry; set `RootLocalizationTable` to choose a table. Missing entries fall back to the source text silently - audit them.
- Text built at runtime (numbers, names) uses `Translator:FormatByKey(key, args)`; get the translator once with `LocalizationService:GetTranslatorForPlayerAsync(player)` (async, wrap in `pcall`, cache it).
- Non-GUI text (billboards built in code, chat lines) is not auto-translated: translate it manually.
- Locale: `player.LocaleId` (the account setting), `LocalizationService.RobloxLocaleId`, `SystemLocaleId`. Country: `GetCountryRegionForPlayerAsync` (async, IP-based, VPNs mislead; never use it for anything security- or payment-critical without a fallback).
- Leave room for longer languages (German and Portuguese run 30%+ longer); avoid text baked into images.
- User-generated text is filtered with `TextService` before display, separately from translation.
- Test with Studio's locale emulation; export/import tables as CSV for bulk edits.

## References
- `references/localization.md` - tables, translators, locale and region detection (roblox-brain).
