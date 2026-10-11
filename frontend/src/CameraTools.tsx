import { useEffect, useState, type RefObject } from "react";
import { Camera, Maximize2, Square } from "lucide-react";
import { t } from "./i18n";

function decodedFrame(panel: HTMLElement | null) {
  const canvas = panel?.querySelector<HTMLCanvasElement>(
    "canvas[data-native-frame][data-ready=true]",
  );
  if (canvas && Date.now() - Number(canvas.dataset.frameAt) < 5000)
    return { source: canvas, width: canvas.width, height: canvas.height };
  const video = panel?.querySelector("video");
  if (
    video &&
    video.readyState >= 2 &&
    !video.paused &&
    video.videoWidth &&
    Date.now() - Number(video.dataset.frameAt) < 5000
  )
    return {
      source: video,
      width: video.videoWidth,
      height: video.videoHeight,
    };
  return null;
}

export function CameraTools({
  panel,
  active,
  stop,
}: {
  panel: RefObject<HTMLElement | null>;
  active: boolean;
  stop: () => void;
}) {
  const [ready, setReady] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    let lastTime = -1;
    const check = () => {
      const video = panel.current?.querySelector("video");
      if (
        active &&
        video &&
        video.currentTime > lastTime &&
        video.readyState >= 2
      ) {
        lastTime = video.currentTime;
        video.dataset.frameAt = String(Date.now());
      }
      setReady(active && !!decodedFrame(panel.current));
    };
    check();
    const timer = setInterval(check, 300);
    return () => clearInterval(timer);
  }, [active, panel]);
  const snapshot = async () => {
    setError("");
    const frame = active && decodedFrame(panel.current);
    if (!frame) return;
    try {
      const canvas = document.createElement("canvas");
      canvas.width = frame.width;
      canvas.height = frame.height;
      canvas.getContext("2d")!.drawImage(frame.source, 0, 0);
      const blob = await new Promise<Blob | null>((resolve) =>
        canvas.toBlob(resolve, "image/jpeg", 0.92),
      );
      if (!blob) throw new Error();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `mammotion-${new Date().toISOString().replace(/[:.]/g, "-")}.jpg`;
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch {
      setError("Snapshot unavailable");
    }
  };
  return (
    <>
      <div className="camera-tools">
        <button disabled={!active} onClick={stop}>
          <Square size={16} />
          {t("Stop streaming")}
        </button>
        <button disabled={!ready} onClick={() => void snapshot()}>
          <Camera size={16} />
          {t("Take snapshot")}
        </button>
        <button
          onClick={() => {
            setError("");
            const target =
              panel.current?.querySelector<HTMLElement>(".video-frame");
            if (!target?.requestFullscreen) {
              setError("Fullscreen unavailable");
              return;
            }
            void target
              .requestFullscreen()
              .catch(() => setError("Fullscreen unavailable"));
          }}
        >
          <Maximize2 size={16} />
          {t("Fullscreen")}
        </button>
      </div>
      <p className="muted">
        {t(
          "Snapshots download to this device. Stopping video does not stop the mower.",
        )}
      </p>
      {error && <p role="alert">{t(error)}</p>}
    </>
  );
}
