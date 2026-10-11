import { CameraTools } from "./CameraTools";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { socketUrl } from "./api";
import { t } from "./i18n";
import { shareFrames } from "./cameraFrames";

export function RelayCamera({ overlay }: { overlay?: ReactNode }) {
  const panel = useRef<HTMLElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const [attempt, setAttempt] = useState(1);
  const [loading, setLoading] = useState(true);
  const [status, setStatus] = useState("Connecting to video provider");
  useEffect(() => {
    if (!attempt) return;
    setLoading(true);
    let disposed = false,
      decoding = false;
    const target = canvas.current;
    if (target) delete target.dataset.ready;
    const stopFrames = shareFrames(target?.parentElement || null);
    const ws = new WebSocket(socketUrl("/camera/relay"));
    ws.binaryType = "arraybuffer";
    setStatus("Connecting to video provider");
    ws.onmessage = async (event) => {
      if (typeof event.data === "string") {
        const message = JSON.parse(event.data);
        if (message.type === "error") {
          setLoading(false);
          setStatus(message.message);
        }
        return;
      }
      if (decoding || disposed) return;
      decoding = true;
      try {
        const image = await createImageBitmap(
          new Blob([event.data], { type: "image/jpeg" }),
        );
        if (!disposed && target) {
          target.width = image.width;
          target.height = image.height;
          target.getContext("2d")?.drawImage(image, 0, 0);
          target.dataset.ready = "true";
          target.dataset.frameAt = String(Date.now());
          setLoading(false);
          setStatus("Live camera");
        }
        image.close();
      } catch {
        if (!disposed) {
          setLoading(false);
          setStatus("Camera connection failed");
        }
      } finally {
        decoding = false;
      }
    };
    ws.onerror = () => {
      setLoading(false);
      setStatus("Camera connection failed");
    };
    ws.onclose = () => {
      if (target && !disposed) delete target.dataset.ready;
      if (!disposed)
        setStatus((old) =>
          old.startsWith("Live") || old === "Connecting to video provider"
            ? "Native video stopped. Reconnect to retry."
            : old,
        );
      stopFrames();
    };
    return () => {
      disposed = true;
      if (target) {
        delete target.dataset.ready;
        target.getContext("2d")?.clearRect(0, 0, target.width, target.height);
      }
      stopFrames();
      ws.close();
    };
  }, [attempt]);
  return (
    <section ref={panel} className="panel camera-panel">
      <div className="section-title">
        <div>
          <span className="eyebrow">{t("FROM THE MOWER")}</span>
          <h2>{t("Live camera")}</h2>
        </div>
      </div>
      <div className="video-frame">
        <canvas
          ref={canvas}
          data-native-frame="true"
          style={{
            width: "100%",
            display: "block",
            height: "100%",
            objectFit: "contain",
          }}
        />
        {overlay}
        {attempt > 0 && loading && (
          <div className="video-loading" aria-live="polite">
            <span className="loading-spinner" />
            <strong>{t("Starting live video…")}</strong>
            <small>{t(status)}</small>
          </div>
        )}
      </div>
      <p role="status">{t(status)}</p>
      <button onClick={() => setAttempt((n) => n + 1)}>
        {t(attempt ? "Reconnect" : "Connect camera")}
      </button>
      <CameraTools
        panel={panel}
        active={attempt > 0}
        stop={() => {
          setAttempt(0);
          setLoading(false);
          setStatus("Streaming stopped");
        }}
      />
    </section>
  );
}
