# LatentPost for Fanvue

Publish your ComfyUI outputs to your Fanvue account. Upload images and videos to your vault and schedule posts without leaving ComfyUI.

- **Your media goes straight from your computer to Fanvue.** LatentPost's server only hands out upload links; it never receives, stores or logs your images, videos or captions.
- **No workflow metadata is uploaded.** ComfyUI's Save Image node embeds your whole workflow and prompts in every PNG. These nodes don't.
- No extra Python packages to install.

## Set up

1. Install **LatentPost for Fanvue** from ComfyUI Manager, or clone this folder into `ComfyUI/custom_nodes/`.
2. Connect your Fanvue creator account at https://latentpost.com/dashboard and create an API key.
3. In ComfyUI, paste the key into **Settings → LatentPost → API key**.
   - Settings aren't saved in workflows, so sharing a workflow or an image never shares your key.
   - Or set the `LATENTPOST_API_KEY` environment variable instead. It wins over Settings. Use it if you run ComfyUI with `--multi-user`, where the nodes can't tell whose settings to read.

## Nodes

Both are in the **LatentPost** category.

### Save to Fanvue Vault

Uploads images, or files already on disk, and files them in a vault folder.

| Input | |
|---|---|
| `images` | Images to upload, as PNG. |
| `file_paths` | Files to upload, such as videos, one path per line. Relative paths are inside ComfyUI's `output` folder. |
| `folder` | Vault folder, created if it doesn't exist. Leave empty for no folder. |
| `filename_prefix` | Images are named `<prefix>_<date>-<time>_<n>.png`. Files keep their own names. |

The output is the Fanvue media IDs, one per line. Connect it to Schedule Fanvue Post.

Supported files: `.png .jpg .jpeg .webp .gif .mp4 .mov .webm .m4v .mp3 .wav .m4a .ogg .flac`, up to 1.5 GB each.

### Schedule Fanvue Post

Creates a post now or at a set time. Fanvue does the scheduling, so ComfyUI doesn't need to stay open. Needs LatentPost Pro.

| Input | |
|---|---|
| `media_uuids` | From Save to Fanvue Vault. Leave unconnected for a text-only post. |
| `text` | The caption, up to 5,000 characters. |
| `audience` | `subscribers`, or `followers-and-subscribers`. |
| `publish_in_minutes` | 0 publishes right away. |
| `publish_at` | A set time like `2026-10-07 18:00`, in your computer's time zone. Add `Z` for UTC. Overrides `publish_in_minutes`. |
| `price_cents` | 0 for a free post. Paid posts start at 300 ($3.00) and need media. |

ComfyUI only re-runs a node when its inputs change. Queueing the same workflow twice won't upload or post twice. To post again, change something, like the caption or the seed.

## When something goes wrong

Error messages are written to be read as they are. The common ones:

- **API key not recognised**: create a new key on the dashboard and paste it into Settings → LatentPost → API key again.
- **Reconnect your account**: your Fanvue sign-in expired. Reconnect on the dashboard; your API key keeps working.
- **Free uploads used up**, or **needs Pro**: upgrade on the dashboard.
- **Fanvue's rate limit**: the node waits and retries by itself.

You can cancel a run at any time, including while it waits for Fanvue.

Only upload content you own the rights to and have consent for. See https://latentpost.com/terms. Questions: support@latentpost.com.

## Development

Run the tests (standard library only, against a fake broker; nothing reaches latentpost.com or Fanvue):

```bash
python -m unittest discover -s tests -t tests
```

Set `LATENTPOST_URL` to point the nodes at another broker, such as `wrangler dev` (`http://localhost:8787`). Plain `http://` works only for localhost.

`tests/e2e_comfyui.py` runs the nodes inside a real ComfyUI, started headless; see its docstring for how to use it.
