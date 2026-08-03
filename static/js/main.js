document.addEventListener('DOMContentLoaded', () => {
    checkAuthStatus();
    loadPackages();
});

function checkAuthStatus() {
    const token = localStorage.getItem('token');
    const user = JSON.parse(localStorage.getItem('user'));
    const authLinks = document.getElementById('auth-links');
    const userInfo = document.getElementById('user-info');
    const usernameDisplay = document.getElementById('username-display');
    const heroBookingsBtn = document.getElementById('hero-bookings-btn');

    if (token && user) {
        if (authLinks) authLinks.classList.add('hidden');
        if (userInfo) userInfo.classList.remove('hidden');
        if (heroBookingsBtn) heroBookingsBtn.classList.remove('hidden');
        
        if (usernameDisplay) {
            const displayName = (user.email && user.email.includes('@')) 
                ? user.email.split('@')[0] 
                : (user.email || user.username || 'User');
            usernameDisplay.textContent = displayName;
        }
    } else {
        if (authLinks) authLinks.classList.remove('hidden');
        if (userInfo) userInfo.classList.add('hidden');
    }
}

async function loadPackages() {
    const container = document.getElementById('packages-container');
    if (!container) return;
    
    try {
        // Cache buster to ensure fresh data
        const response = await fetch('/packages?v=' + Date.now());
        const data = await response.json();
        
        if (data.success) {
            container.innerHTML = '';
            data.data.forEach(pkg => {
                const status = (pkg.availability_status || '').trim();
                const isAvailable = status === 'Available';
                console.log(`Package: ${pkg.package_name}, Raw Status: "${pkg.availability_status}", Clean Status: "${status}", isAvailable: ${isAvailable}`);
                
                const statusColor = isAvailable ? 'text-green-400 bg-green-500/10' : 'text-red-400 bg-red-500/10';
                
                const imageUrl = pkg.image_url || getPackageImage(pkg);
                
                const card = `
                    <div class="glass rounded-[2.5rem] overflow-hidden card-hover group ${!isAvailable ? 'opacity-75 grayscale-[0.2]' : ''}">
                        <div class="h-64 relative overflow-hidden">
                            <img src="${imageUrl}" alt="${pkg.package_name}" class="w-full h-full object-cover group-hover:scale-110 transition duration-700">
                            <div class="absolute inset-0 bg-gradient-to-t from-[#0a0f1d] to-transparent opacity-60"></div>
                            ${!isAvailable ? `
                                <div class="absolute inset-0 bg-slate-900/40 backdrop-blur-[1px] flex items-center justify-center">
                                    <span class="bg-red-500/80 text-white px-6 py-2 rounded-full font-black text-sm uppercase tracking-widest shadow-xl">Fully Booked</span>
                                </div>
                            ` : ''}
                            <div class="absolute top-6 left-6 ${statusColor} px-3 py-1 rounded-full text-[10px] font-black uppercase tracking-widest border border-current">
                                ${pkg.availability_status}
                            </div>
                            <div class="absolute top-6 right-6 glass px-4 py-2 rounded-full text-xs font-black uppercase tracking-widest text-white">
                                ${pkg.duration} Days
                            </div>
                        </div>
                        <div class="p-8">
                            <div class="flex justify-between items-start mb-4">
                                <div>
                                    <h3 class="text-2xl font-black text-white group-hover:text-blue-400 transition">${pkg.package_name}</h3>
                                    <p class="text-blue-500 text-sm font-bold uppercase tracking-wider mt-1">${pkg.destination}</p>
                                </div>
                            </div>
                            <p class="text-gray-400 mb-8 line-clamp-3 text-sm leading-relaxed">${pkg.description}</p>
                            <div class="flex items-center justify-between pt-6 border-t border-white/5">
                                <div class="flex flex-col">
                                    <span class="text-gray-500 text-[10px] font-bold uppercase tracking-widest">From</span>
                                    <span class="text-2xl font-black text-white" style="font-family: 'Inter', sans-serif;">
                                        R ${pkg.price.toLocaleString()}
                                    </span>
                                </div>
                                <button onclick="handleBooking(${pkg.package_id}, ${!isAvailable})" 
                                    class="${isAvailable ? 'bg-blue-600 hover:bg-blue-700' : 'bg-slate-800 hover:bg-slate-700'} text-white px-8 py-3 rounded-2xl font-black transition shadow-lg shadow-blue-600/20">
                                    ${isAvailable ? 'Book Now' : 'View Details'}
                                </button>
                            </div>
                        </div>
                    </div>
                `;
                container.innerHTML += card;
            });
        }
    } catch (error) {
        console.error('Error loading packages:', error);
        container.innerHTML = '<p class="text-red-500 text-center col-span-full">Failed to load packages. Please try again later.</p>';
    }
}

function handleBooking(packageId, viewOnly = false) {
    const token = localStorage.getItem('token');
    if (!token && !viewOnly) {
        const modal = document.getElementById('booking-modal');
        modal.classList.remove('hidden');
        modal.classList.add('flex');
    } else {
        window.location.href = `booking.html?packageId=${packageId}${viewOnly ? '&viewOnly=true' : ''}`;
    }
}

function closeModal() {
    const modal = document.getElementById('booking-modal');
    modal.classList.add('hidden');
    modal.classList.remove('flex');
}

function logout() {
    localStorage.removeItem('token');
    localStorage.removeItem('user');
    window.location.href = 'index.html';
}
