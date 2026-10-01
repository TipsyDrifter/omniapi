"""Capability layer — non-image modalities for OmniAPI MCP.

Parallel to the image-generation `providers/` layer. Each capability defines
its own provider interface (input/output shapes differ per modality), but they
share the generic `ProviderConfig` / `ProviderError` plumbing from
`providers.base`. This keeps image generation untouched while letting new
modalities (transcription, text, speech, ...) plug in independently.
"""
