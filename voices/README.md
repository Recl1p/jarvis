# The voice pack

`jarvis-voices.enc` holds the Pocket TTS voice states for JARVIS, Ultron and Cortana (about 18 MB), encrypted with
AES-256 (OpenSSL `enc -aes-256-cbc -pbkdf2 -iter 600000 -md sha256`). It is copied into the image as is.

At container start `image/cont-init.d/15-jarvis-voices` unlocks it with `JARVIS_VOICE_KEY` (set in the compose file the
owner shares) into `$HERMES_HOME/vesper-voices/pack/`, checked against the pack's own SHA-256 list. Pocket TTS then
speaks those states with its public model: no voice-cloning weights and no Hugging Face account are needed, and the
audio is identical, sample for sample, to the voices the states were exported from. Without the key, or with a wrong
one, JARVIS starts normally with its original voices.

The key is not in this repository. Keep it with the compose files you give out, and only share those privately.
