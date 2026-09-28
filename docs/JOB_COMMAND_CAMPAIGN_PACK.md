# Job Command Campaign Pack

## Campaign identity

| Field | Definition |
| --- | --- |
| Product | **Job Command** — an operator-led, multilingual live voice experience in the connected web application. |
| Identity source | The operator’s explicitly authorized avatar and voice profile only. |
| Delivery | Direct, calm, decisive, welcoming, and plainspoken. |
| Audience experience | Recognize the caller’s chosen/supported language, respond with a language-appropriate authorized voice preset, and provide clear help without intimidation or imitation. |
| Primary channels | Authenticated web voice session first; phone/telephony is an integration-ready future surface. |

## Creative boundary

This campaign can be confident and commercially direct without borrowing a specific person’s identity or creative output.

**Allowed directional traits**

- concise, grounded language;
- earned-authority tone;
- clear operational steps;
- structured, low-hype explanations;
- culturally respectful language-specific greetings and pronunciation.

**Excluded**

- a public person’s face, voice, name, implied endorsement, scripts, hooks, catchphrases, cadence, content sequencing, or reverse-engineered content blueprint;
- caller biometric identification or attempts to determine who someone is from their voice;
- accent caricature, class-based signaling, pressure tactics, or unreviewed claims.

## Core call behavior

1. **Open:** State what Job Command can help with and optionally invite the caller to select a language.
2. **Clarify:** Ask one concise question at a time; echo back the caller’s goal.
3. **Resolve:** Give a plain-language answer, a next step, and any critical constraint.
4. **Escalate:** Hand off when there is uncertainty, a regulated/high-impact request, account access, payment, or an issue outside the approved knowledge base.
5. **Close:** Summarize the next action and never claim a completed external action that has not occurred.

## Language and voice expectations

- The agent supports only languages explicitly configured in `JOB_COMMAND_SUPPORTED_LANGUAGES` and in the ElevenLabs Agent configuration.
- The first-session language may be chosen by the caller or selected by the provider’s configured detection tool. For mixed/noisy audio, explicit user selection wins.
- Each target language should have a native-review-approved opening message and a language-appropriate authorized voice preset.
- The system does not promise that the operator’s exact accent will transfer perfectly to every language. Review pronunciation and accent fidelity by language, then use a provider-approved language-specific voice preset when needed.

## Measurement plan

| Metric | Definition | Guardrail |
| --- | --- | --- |
| Session start success | Signed session issued → client conversation started | Do not log signed URL. |
| First response latency | Client opening turn to agent audio response | Measure provider timing, not raw audio content. |
| Language match rate | Resolved language matches caller confirmation | Provide manual language fallback. |
| Completion rate | Started sessions that reach provider completion | Exclude user-cancelled sessions from quality failure rate. |
| Safe escalation rate | Appropriate handoff for unsupported/high-impact requests | Review misses, not just volume. |
| Consent rate | Sessions with explicit archive consent | Archive only consented audio. |
| Transcript quality | Native-speaker QA for target language/accents | Separate ElevenLabs live text from Gemini archive output. |

## Launch sequence

1. Configure the operator-approved ElevenLabs Agent and language presets.
2. Set deployment secrets and enable `JOB_COMMAND_OPERATOR_AUTHORIZED=true`.
3. Connect the authenticated Vercel route to `POST /voice/sessions`.
4. Configure the signed ElevenLabs post-call webhook.
5. Use mock provider tests first, then a small internal-call pilot for each language.
6. Review signed URL handling, consent-off behavior, language switches, archive transcript output, latency, and escalation.
7. Expand language coverage only after language-specific review passes.
