import { describe, expect, it } from "vitest";
import { backupVerificationLabel } from "../pages/System";


describe("System backup verification labels", () => {
  it("reports a valid remote-dependent archive as not portable by itself", () => {
    expect(backupVerificationLabel({
      valid: true,
      files: 19,
      media_self_contained: false,
      cloud_primary_media: {
        applicable: true,
        valid: true,
        dependency_count: 2,
        requires_remote: true,
        remote_availability_checked: false,
        message: "Remote availability and credentials were not tested.",
      },
    })).toBe(
      "Verified (19 files); not portable by itself — depends on 2 " +
      "cloud-primary media objects at the configured remote",
    );
  });

  it("distinguishes a self-contained media archive", () => {
    expect(backupVerificationLabel({
      valid: true,
      files: 23,
      media_self_contained: true,
      cloud_primary_media: {
        applicable: true,
        valid: true,
        dependency_count: 0,
        requires_remote: false,
        remote_availability_checked: false,
        message: "All selected media bytes are contained in the archive.",
      },
    })).toBe("Verified (23 files); selected media is self-contained");
  });
});
