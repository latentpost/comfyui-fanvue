// Adds Settings → LatentPost → API key. ComfyUI saves it in user/default/comfy.settings.json, where
// client.py reads it. Settings aren't saved in workflows, so the key never ends up in a shared PNG.
import { app } from "../../scripts/app.js";

app.registerExtension({
  name: "LatentPost.Settings",
  settings: [
    {
      id: "LatentPost.APIKey", // client.SETTING_ID
      category: ["LatentPost", "Account", "APIKey"],
      name: "API key",
      tooltip: "Create one at latentpost.com/dashboard. The nodes read it each time they run.",
      type: "text",
      defaultValue: "",
      attrs: { type: "password", autocomplete: "off", spellcheck: false, placeholder: "fvc_…" },
      telemetry: { trackChanges: false }, // never report changes to this setting, let alone its value
    },
  ],
});
