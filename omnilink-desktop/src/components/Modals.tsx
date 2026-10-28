import { McpModal } from "@/components/modals/McpModal";
import { ProjectModal } from "@/components/modals/ProjectModal";
import { PromptModal } from "@/components/modals/PromptModal";
import { GoogleAuthModal } from "@/components/modals/GoogleAuthModal";
import { ModelSelectorModal } from "@/components/modals/ModelSelectorModal";
import { useApp } from "@/store/app";

export function ModalHost() {
  const modal = useApp((state) => state.modal);
  const closeModal = useApp((state) => state.closeModal);

  return (
    <>
      <ProjectModal open={modal?.type === "project-new"} />
      <PromptModal
        open={modal?.type === "prompt-new" || modal?.type === "prompt-edit"}
        promptId={modal?.type === "prompt-edit" ? modal.id : undefined}
      />
      <McpModal
        open={modal?.type === "mcp-new" || modal?.type === "mcp-edit"}
        serverId={modal?.type === "mcp-edit" ? modal.id : undefined}
      />
      <GoogleAuthModal
        open={modal?.type === "google-auth"}
        url={modal?.type === "google-auth" ? modal.url : ""}
      />
      <ModelSelectorModal
        open={modal?.type === "model-selector"}
        onClose={closeModal}
      />
    </>
  );
}
