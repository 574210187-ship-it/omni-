# Feishu append/readback contract

The existing Feishu MCP is the only remote writer. This package creates the
three validated local artifacts and `feishu_append_contract.json`; it never
stores credentials or makes a hidden remote write.

1. Lock exactly one record and obtain the input bytes through the approved
   bridge. Verify local SHA-256 and video metadata.
2. Run this package. A `cut_conflict.json` means HOLD: do not upload media,
   do not alter the record, and do not manufacture fallback output.
3. On PASS append exactly `videos_and_screenshot.zip`,
   `supporting_materials.zip`, and `文案.txt` to the approved output field.
   Never delete or replace prior attachments.
4. Read the exact record back. Verify all three new remote files by filename,
   byte count, and SHA-256 before reporting success.

The execution code contains no connector secret, remote credential, or
alternative uploader.
