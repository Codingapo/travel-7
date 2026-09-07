const PACKAGE_IMAGE_BY_NAME = {
    "Cape Town": "https://images.unsplash.com/photo-1580619305218-8423a7ef79b4?q=80&w=1400&auto=format&fit=crop",
    "Zanzibar, Jambiani": "https://images.unsplash.com/photo-1586861635167-e5223aadc9fe?q=80&w=1400&auto=format&fit=crop",
    "Zanzibar, Nungwi": "https://images.unsplash.com/photo-1519046904884-53103b34b206?q=80&w=1400&auto=format&fit=crop",
    "Zanzibar, Paje": "https://images.unsplash.com/photo-1537996194471-e657df975ab4?q=80&w=1400&auto=format&fit=crop",
    "Namibia, Swakopmund": "https://images.unsplash.com/photo-1581006852262-e4307cf6283a?q=80&w=1400&auto=format&fit=crop",
    "Zambia, Livingston": "https://images.unsplash.com/photo-1547471080-7cc2caa01a7e?q=80&w=1400&auto=format&fit=crop",
    "Dubai (4 Star)": "https://images.unsplash.com/photo-1512453979798-5ea266f8880c?q=80&w=1400&auto=format&fit=crop",
    "Dubai (5 Star)": "https://images.unsplash.com/photo-1518684079-3c830dcef090?q=80&w=1400&auto=format&fit=crop",
    "Bali, Seminyak": "https://images.unsplash.com/photo-1537953773345-d172ccf13cf1?q=80&w=1400&auto=format&fit=crop",
    "Bali, Seminyak & Ubud": "https://images.unsplash.com/photo-1537953773345-d172ccf13cf1?q=80&w=1400&auto=format&fit=crop",
    "Singapore & Bali": "https://images.unsplash.com/photo-1525625293386-3f8f99389edd?q=80&w=1400&auto=format&fit=crop",
    "Thailand; Phuket": "https://images.unsplash.com/photo-1589308078059-be1415eab4c3?q=80&w=1400&auto=format&fit=crop",
    "Thailand; Phuket & Bangkok": "https://images.unsplash.com/photo-1552465011-b4e21bf6e79a?q=80&w=1400&auto=format&fit=crop",
    "Mauritius": "https://images.unsplash.com/photo-1514282401047-d79a71a590e8?q=80&w=1400&auto=format&fit=crop",
};

const PACKAGE_IMAGE_BY_DESTINATION = {
    "Cape Town": "https://images.unsplash.com/photo-1580619305218-8423a7ef79b4?q=80&w=1400&auto=format&fit=crop",
    "Zanzibar": "https://images.unsplash.com/photo-1586861635167-e5223aadc9fe?q=80&w=1400&auto=format&fit=crop",
    "Namibia": "https://images.unsplash.com/photo-1581006852262-e4307cf6283a?q=80&w=1400&auto=format&fit=crop",
    "Zambia": "https://images.unsplash.com/photo-1547471080-7cc2caa01a7e?q=80&w=1400&auto=format&fit=crop",
    "Bali": "https://images.unsplash.com/photo-1537953773345-d172ccf13cf1?q=80&w=1400&auto=format&fit=crop",
    "Thailand": "https://images.unsplash.com/photo-1528181304800-2f140819ad9c?q=80&w=1400&auto=format&fit=crop",
    "Mauritius": "https://images.unsplash.com/photo-1514282401047-d79a71a590e8?q=80&w=1400&auto=format&fit=crop",
};

function getPackageImage(pkg) {
    if (!pkg) {
        return "https://images.unsplash.com/photo-1488646953014-85cb44e25828?q=80&w=1400&auto=format&fit=crop";
    }
    return (
        PACKAGE_IMAGE_BY_NAME[pkg.package_name] ||
        PACKAGE_IMAGE_BY_DESTINATION[pkg.destination] ||
        "https://images.unsplash.com/photo-1488646953014-85cb44e25828?q=80&w=1400&auto=format&fit=crop"
    );
}
