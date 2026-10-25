export interface VolumeWritingSummary {
  scenes: number; chapters: number; written: number; missing_plans: number; pending_bodies: number;
  complete_planning?: boolean; arcs?: number; missing_arc_plots?: number; arcs_without_scenes?: number; volume_plot_missing?: boolean;
}

export interface VolumeWritingJob {
  id: string; novel_id: string; volume_id: string; volume_title?: string;
  status: "running" | "paused" | "failed" | "completed" | "cancelled"; active: boolean;
  pause_requested: boolean; cancel_requested: boolean; summary: VolumeWritingSummary;
  created_chapters: number; written_chapters: number; skipped_chapters: number; completed_scenes: number;
  has_saved_candidate: boolean;
  complete_planning?: boolean; completed_arcs?: number; created_arcs?: number; created_scenes?: number;
  current: { stage: "volume_plot" | "volume_preparing" | "arc_planning" | "arc_plot" | "scene_planning" | "planning" | "writing" | "reviewing" | "saving" | "fact_extracting" | "fact_reviewing" | "fact_saving"; title: string; node: string } | null;
  error: { message: string; field_errors?: Record<string, string[]> } | null;
  created_at: string; updated_at: string;
}
