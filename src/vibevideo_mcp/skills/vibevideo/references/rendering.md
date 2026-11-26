# Setting up a video production environment

Set up the renderer once, in the project environment from which you launch Claude Code or Codex. Reuse it across productions. Choose the stack that matches the brief. The examples below build a local environment similar to the tools used for production; versions and fonts affect visual results.

## Encode, mix, and draw text

On Ubuntu or Debian with package administration access:

```sh
sudo apt-get update
sudo apt-get install ffmpeg imagemagick sox fontconfig fonts-noto-cjk
```

On macOS with Homebrew:

```sh
brew install ffmpeg imagemagick sox fontconfig
```

Install a suitable CJK font, such as Noto Sans CJK, if rendering Mandarin, Japanese or Korean text. Choose an explicit font file in the render script. Use FFmpeg builds with the libx264, AAC and libopus encoders. From the installed VibeVideo environment, validate the tools locally:

```sh
vibevideo doctor
ffmpeg -hide_banner -encoders 2>/dev/null | grep -E 'libx264|aac|libopus'
fc-match 'sans-serif:lang=zh'
```

For Python-drawn frames or audio composition, activate the project's virtual environment and install the libraries the render script imports. A typical stack is:

```sh
python -m pip install Pillow numpy scipy soundfile
```

## Browser animation and 3D

For HTML/Canvas, WebGL or Three.js, use a current Node.js LTS release and npm. In the video project, create a package manifest if it has none, install the dependencies, and install the Chromium build matched to Playwright. The example pins Three.js r160 to match the revision used for the experiments. Keep the npm lockfile with the project.

```sh
test -f package.json || npm init -y
npm install playwright three@0.160.0 gsap
npx playwright install chromium
```

On a fresh supported Linux host, use `npx playwright install --with-deps chromium` to include browser system dependencies. If the render script uses Python Playwright, install its own matching browser with `python -m pip install playwright` and `python -m playwright install chromium`. Serve ES modules over a local HTTP origin and set a fixed viewport, animation clock and frame rate. Software WebGL can render headlessly when a GPU is unavailable. [Playwright browsers](https://playwright.dev/docs/browsers)

For a one-time offline encode check, run this in a scratch directory:

```sh
ffmpeg -n -v error -f lavfi -i testsrc2=size=320x180:rate=24 -f lavfi -i sine=frequency=440 -t 1 -c:v libx264 -pix_fmt yuv420p -c:a aac render-check.mp4
ffprobe -v error -show_entries stream=codec_name,width,height -of json render-check.mp4
```

## Optional narration tools

For projects that need speech synthesis, we recommend installing Edge TTS in the rendering environment:

```sh
python -m pip install edge-tts
edge-tts --help
```

Speech synthesis uses an online service. For an offline project, use a locally available synthesizer. [Edge TTS](https://github.com/rany2/edge-tts)
