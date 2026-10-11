import { reportNetworkFailure } from "./api";

// Capture only decoded video, never HA placeholder images. Frames remain in server memory.
export function shareFrames(container: HTMLElement | null) {
  let pending = false;
  let identity: any = null;
  void Promise.all([
    fetch("/api/state").then((r) => r.json()),
    fetch("/api/settings").then((r) => r.json()),
  ])
    .then(([state, settings]) => {
      if (settings.preferences.telegram_enabled) identity = state;
    })
    .catch((error) => {
      reportNetworkFailure(error);
    });
  const timer = setInterval(async () => {
    const video = container?.querySelector("video");
    const native = container?.querySelector<HTMLCanvasElement>(
      "canvas[data-native-frame][data-ready=true]",
    );
    const source = native || video;
    const width = native?.width || video?.videoWidth || 0;
    const height = native?.height || video?.videoHeight || 0;
    if (
      !identity?.device ||
      pending ||
      !source ||
      (!native && (!video || video.readyState < 2)) ||
      !width ||
      !height
    )
      return;
    pending = true;
    try {
      const canvas = document.createElement("canvas");
      canvas.width = Math.min(640, width);
      canvas.height = Math.round((canvas.width * height) / width);
      canvas
        .getContext("2d")
        ?.drawImage(source, 0, 0, canvas.width, canvas.height);
      const blob = await new Promise<Blob | null>((resolve) =>
        canvas.toBlob(resolve, "image/jpeg", 0.75),
      );
      if (blob) {
        const response = await fetch("/api/camera/frame", {
          method: "POST",
          headers: {
            "X-Console-Request": "1",
            "Content-Type": "image/jpeg",
            "X-Camera-Provider": identity.provider,
            "X-Camera-Device": identity.device.entity_id,
          },
          body: blob,
        });
        if (response.status === 422) clearInterval(timer);
      }
    } catch (error) {
      reportNetworkFailure(error);
      /* Frame availability must never interrupt video. */
    } finally {
      pending = false;
    }
  }, 5000);
  return () => clearInterval(timer);
}
