const PACKAGE_IMAGE_FALLBACK = "/static/images/package-fallback.svg";

function getPackageImage(pkg) {
    return (pkg && typeof pkg.image_url === "string" && /^https?:\/\//i.test(pkg.image_url))
        ? pkg.image_url
        : PACKAGE_IMAGE_FALLBACK;
}
