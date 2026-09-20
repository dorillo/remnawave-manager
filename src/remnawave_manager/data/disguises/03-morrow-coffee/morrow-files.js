export async function avatar(file) {
  if (!/^image\/(png|jpeg|webp)$/.test(file.type))
    throw new Error("avatarError");
  const bitmap = await createImageBitmap(file);
  try {
    const canvas = document.createElement("canvas");
    canvas.width = canvas.height = 160;
    const context = canvas.getContext("2d");
    const size = Math.min(bitmap.width, bitmap.height);
    context.drawImage(
      bitmap,
      (bitmap.width - size) / 2,
      (bitmap.height - size) / 2,
      size,
      size,
      0,
      0,
      160,
      160,
    );
    return canvas.toDataURL("image/jpeg", 0.8);
  } finally {
    bitmap.close();
  }
}
export function download(name, text, type = "text/plain") {
  const url = URL.createObjectURL(
    new Blob([text], { type: `${type};charset=utf-8` }),
  );
  const link = document.createElement("a");
  link.href = url;
  link.download = name.replace(/[<>:"/\\|?*\x00-\x1f]/g, "_").slice(0, 160);
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
}
