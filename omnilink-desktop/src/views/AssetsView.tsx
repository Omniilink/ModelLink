import { Image as ImageIcon, Trash2, Upload } from "lucide-react";
import { useRef, useState } from "react";

import { IconButton } from "@/components/ui/IconButton";
import { useApp } from "@/store/app";

export function AssetsView() {
  const assets = useApp((state) => state.assets);
  const addAsset = useApp((state) => state.addAsset);
  const deleteAsset = useApp((state) => state.deleteAsset);
  const pushToast = useApp((state) => state.pushToast);
  const [name, setName] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);

  const handleFile = (file: File) => {
    const url = URL.createObjectURL(file);
    const kind = file.type.startsWith("image/")
      ? "image"
      : file.name.match(/\.(glb|gltf|obj|stl)$/i)
        ? "model"
        : "other";
    addAsset({ name: name.trim() || file.name, kind, path: url });
    setName("");
    pushToast({ title: "Asset added", tone: "success" });
  };

  return (
    <div className="flex h-full flex-col">
      <div className="flex h-[52px] flex-none items-center gap-3 border-b border-line px-5">
        <span className="flex h-7 w-7 flex-none items-center justify-center rounded-md border border-line bg-[var(--bg-elevated)] text-muted">
          <ImageIcon size={14} />
        </span>
        <div className="min-w-0 flex-1">
          <h1 className="text-[13px] font-semibold tracking-[-0.01em] text-fg">
            Assets
          </h1>
          <p className="mt-0.5 text-[10px] text-subtle">
            {assets.length} asset{assets.length === 1 ? "" : "s"} in this
            workspace
          </p>
        </div>
        <label className="btn btn-primary cursor-pointer">
          <Upload size={13} />
          Add asset
          <input
            ref={fileRef}
            type="file"
            accept="image/*,.glb,.gltf,.obj,.stl"
            className="hidden"
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (file) handleFile(file);
              event.target.value = "";
            }}
          />
        </label>
      </div>

      <div className="scroll-thin min-h-0 flex-1 overflow-y-auto px-5 py-4">
        {assets.length === 0 ? (
          <div className="flex h-full flex-col items-center justify-center text-center">
            <ImageIcon size={40} className="text-subtle" />
            <p className="mt-3 text-[12.5px] text-muted">
              No assets yet. Add images or 3D models.
            </p>
          </div>
        ) : (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
            {assets.map((asset) => (
              <div
                key={asset.id}
                className="group relative overflow-hidden rounded-xl border border-line bg-[var(--bg-surface)]"
              >
                {asset.kind === "image" ? (
                  <img
                    src={asset.path}
                    alt={asset.name}
                    className="aspect-square w-full object-cover"
                  />
                ) : (
                  <div className="flex aspect-square w-full items-center justify-center text-subtle">
                    <ImageIcon size={28} />
                  </div>
                )}
                <div className="flex items-center justify-between px-2.5 py-2">
                  <span className="truncate text-[10.5px] font-medium text-fg">
                    {asset.name}
                  </span>
                  <IconButton
                    label="Remove asset"
                    onClick={() => deleteAsset(asset.id)}
                    className="h-5 w-5 opacity-0 transition-opacity group-hover:opacity-100"
                  >
                    <Trash2 size={11} />
                  </IconButton>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
