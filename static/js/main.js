document.addEventListener('DOMContentLoaded', () => {
    checkAuthStatus();
    loadPackages();
    initPasswordToggles();
    initPasswordValidation();

    const sortSelect = document.getElementById('package-sort');
    if (sortSelect) {
        sortSelect.addEventListener('change', () => loadPackages(sortSelect.value));
    }
});

// ============================================================
// PASSWORD STRENGTH VALIDATION (FRONTEND)
// Mirrors the backend validate_password_strength in app.py
// ============================================================

const PASSWORD_MIN_LENGTH = 12;
const PASSWORD_SPECIAL_REGEX = /[!@#$%^&*()_+\-=\[\]{}|;:,.<>?/~`]/;

function getPasswordRequirements() {
    return [
        { key: 'length', label: `At least ${PASSWORD_MIN_LENGTH} characters`, test: (p) => p.length >= PASSWORD_MIN_LENGTH },
        { key: 'upper', label: 'One uppercase letter (A-Z)', test: (p) => /[A-Z]/.test(p) },
        { key: 'lower', label: 'One lowercase letter (a-z)', test: (p) => /[a-z]/.test(p) },
        { key: 'number', label: 'One number (0-9)', test: (p) => /[0-9]/.test(p) },
        { key: 'special', label: 'One special character (!@#$%^&*)', test: (p) => PASSWORD_SPECIAL_REGEX.test(p) },
    ];
}

function validatePasswordStrength(password, forbiddenSubstrings = []) {
    const errors = [];
    if (!password) {
        return { valid: false, errors: ['Password is required.'], requirements: getPasswordRequirements().map(r => ({ ...r, met: false })) };
    }

    const requirements = getPasswordRequirements().map(r => ({ ...r, met: r.test(password) }));
    requirements.forEach(r => { if (!r.met) errors.push(r.label); });

    if (forbiddenSubstrings && forbiddenSubstrings.length > 0) {
        const pwdLower = password.toLowerCase();
        const found = forbiddenSubstrings.some(tok => {
            if (!tok || typeof tok !== 'string') return false;
            const t = tok.trim().toLowerCase();
            return t && t.length >= 3 && pwdLower.includes(t);
        });
        if (found) {
            errors.push('Must not contain your username or email address');
            requirements.push({ key: 'notPersonal', label: 'Must not contain your username or email address', met: false });
        }
    }

    return { valid: errors.length === 0, errors, requirements };
}

function buildPasswordRequirementsEl(inputEl) {
    const wrapper = document.createElement('div');
    wrapper.className = 'password-requirements mt-3 p-3 rounded-xl border border-white/10 bg-white/[0.02] hidden';
    wrapper.setAttribute('data-pw-req-wrapper', 'true');

    const header = document.createElement('p');
    header.className = 'text-[11px] uppercase tracking-widest font-black text-gray-400 mb-2';
    header.textContent = 'Password Requirements';
    wrapper.appendChild(header);

    const list = document.createElement('ul');
    list.className = 'space-y-1.5';
    wrapper.appendChild(list);

    getPasswordRequirements().forEach(req => {
        const li = document.createElement('li');
        li.className = 'flex items-center gap-2 text-xs text-gray-400';
        li.setAttribute('data-pw-req', req.key);
        li.innerHTML = `
            <i class="fas fa-circle text-[6px] opacity-50 req-icon" data-icon="pending"></i>
            <span class="req-label">${req.label}</span>
        `;
        list.appendChild(li);
    });

    const personalLi = document.createElement('li');
    personalLi.className = 'flex items-center gap-2 text-xs text-gray-400 hidden';
    personalLi.setAttribute('data-pw-req', 'notPersonal');
    personalLi.innerHTML = `
        <i class="fas fa-circle text-[6px] opacity-50 req-icon" data-icon="pending"></i>
        <span class="req-label">Must not contain your username or email address</span>
    `;
    list.appendChild(personalLi);

    if (inputEl.parentNode) {
        inputEl.parentNode.after(wrapper);
    }
    return wrapper;
}

function updatePasswordRequirementsUI(wrapper, password, forbiddenSubstrings) {
    if (!wrapper) return;
    const result = validatePasswordStrength(password, forbiddenSubstrings);
    wrapper.classList.toggle('hidden', !password);
    if (!password) return;

    const allReqs = result.requirements;
    allReqs.forEach(req => {
        const li = wrapper.querySelector(`[data-pw-req="${req.key}"]`);
        if (!li) return;
        if (req.key === 'notPersonal') {
            li.classList.remove('hidden');
        }
        const icon = li.querySelector('.req-icon');
        if (icon) {
            if (req.met) {
                icon.className = 'fas fa-check req-icon text-teal-400';
                icon.setAttribute('data-icon', 'ok');
            } else {
                icon.className = 'fas fa-circle text-[6px] opacity-50 req-icon';
                icon.setAttribute('data-icon', 'pending');
            }
        }
        const label = li.querySelector('.req-label');
        if (label) {
            label.className = `req-label ${req.met ? 'text-teal-300 font-semibold' : 'text-gray-400'}`;
        }
    });
}

function findAssociatedIdentityInputs(inputEl) {
    const form = inputEl.closest('form');
    if (!form) return [];
    const candidates = [
        form.querySelector('input[type="email"]'),
        form.querySelector('#email'),
        form.querySelector('#username'),
        form.querySelector('#signup-email'),
        form.querySelector('#login-email'),
        form.querySelector('#admin-email'),
    ].filter(Boolean);
    const values = new Set();
    candidates.forEach(el => {
        const v = (el.value || '').trim();
        if (v) {
            values.add(v);
            if (v.includes('@')) values.add(v.split('@')[0]);
        }
    });
    return [...values];
}

function initPasswordValidation() {
    const styleId = 'pw-validation-styles';
    if (!document.getElementById(styleId)) {
        const style = document.createElement('style');
        style.id = styleId;
        style.textContent = `
            .password-requirements[data-pw-req-wrapper="true"] { transition: opacity .2s ease, transform .2s ease; }
            .password-strength-meter { height: 4px; border-radius: 999px; overflow: hidden; background: rgba(255,255,255,0.08); margin-top: 10px; display: none; }
            .password-strength-meter > span { display: block; height: 100%; width: 0%; transition: width .3s ease, background-color .3s ease; border-radius: 999px; }
            .password-mismatch-hint { font-size: 11px; font-weight: 800; color: #f87171; margin-top: 6px; display: none; }
            .password-mismatch-hint.ok { color: #2dd4bf; }
        `;
        document.head.appendChild(style);
    }

    const newPasswordInputs = document.querySelectorAll(
        'input[type="password"][id="signup-password"], ' +
        'input[type="password"][id="new"], ' +
        'input[type="password"][id="password"]:not(#login-password):not(#confirmPassword):not(#current)'
    );

    newPasswordInputs.forEach(input => {
        if (input.dataset.pwValidateAttached === 'true') return;
        input.dataset.pwValidateAttached = 'true';

        const wrapper = buildPasswordRequirementsEl(input);

        const meter = document.createElement('div');
        meter.className = 'password-strength-meter';
        meter.innerHTML = '<span></span>';
        if (input.parentNode) input.parentNode.after(meter);

        input.addEventListener('input', () => {
            const forbidden = findAssociatedIdentityInputs(input);
            updatePasswordRequirementsUI(wrapper, input.value, forbidden);

            const result = validatePasswordStrength(input.value, forbidden);
            const metCount = result.requirements.filter(r => r.met).length;
            const total = result.requirements.length;
            const pct = total > 0 ? (metCount / total) * 100 : 0;
            meter.style.display = input.value ? 'block' : 'none';
            const bar = meter.querySelector('span');
            if (bar) {
                bar.style.width = `${pct}%`;
                if (pct < 40) bar.style.backgroundColor = '#ef4444';
                else if (pct < 80) bar.style.backgroundColor = '#f59e0b';
                else bar.style.backgroundColor = '#14b8a6';
            }

            const confirmId = input.dataset.confirmId;
            const confirmInput = confirmId ? document.getElementById(confirmId) : null;
            if (confirmInput && confirmInput.value) {
                confirmInput.dispatchEvent(new Event('input'));
            }
        });
    });

    const confirmInputs = [
        { pwd: 'signup-password', confirm: 'signup-confirm' },
        { pwd: 'new', confirm: 'confirm' },
        { pwd: 'password', confirm: 'confirm-password' },
    ];

    confirmInputs.forEach(pair => {
        const pwdInput = document.getElementById(pair.pwd);
        const confirmInput = document.getElementById(pair.confirm);
        if (!pwdInput || !confirmInput) return;

        if (pwdInput.dataset.pwValidateAttached === 'true') {
            pwdInput.dataset.confirmId = pair.confirm;
        }

        let hint = confirmInput.parentNode.querySelector('.password-mismatch-hint');
        if (!hint) {
            hint = document.createElement('p');
            hint.className = 'password-mismatch-hint';
            hint.textContent = 'Passwords do not match.';
            if (confirmInput.parentNode) confirmInput.parentNode.after(hint);
        }

        function checkMatch() {
            if (!confirmInput.value && !pwdInput.value) {
                hint.style.display = 'none';
                hint.classList.remove('ok');
                return;
            }
            if (!confirmInput.value) {
                hint.style.display = 'none';
                hint.classList.remove('ok');
                return;
            }
            if (pwdInput.value === confirmInput.value) {
                hint.style.display = 'block';
                hint.classList.add('ok');
                hint.textContent = 'Passwords match.';
            } else {
                hint.style.display = 'block';
                hint.classList.remove('ok');
                hint.textContent = 'Passwords do not match.';
            }
        }

        pwdInput.addEventListener('input', checkMatch);
        confirmInput.addEventListener('input', checkMatch);
    });
}


function initPasswordToggles() {
    const styleId = 'pw-toggle-styles';
    if (!document.getElementById(styleId)) {
        const style = document.createElement('style');
        style.id = styleId;
        style.textContent = `
            .pw-toggle-wrapper { position: relative; }
            .pw-toggle-btn {
                position: absolute;
                right: 12px;
                top: 50%;
                transform: translateY(-50%);
                background: transparent;
                border: none;
                cursor: pointer;
                padding: 6px 8px;
                color: #64748b;
                z-index: 5;
                display: flex;
                align-items: center;
                justify-content: center;
                border-radius: 8px;
                transition: color 0.2s ease, background 0.2s ease;
            }
            .pw-toggle-btn:hover {
                color: #94a3b8;
                background: rgba(255, 255, 255, 0.05);
            }
            .pw-toggle-btn:focus {
                outline: none;
                box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.25);
            }
            .pw-toggle-btn svg {
                width: 18px;
                height: 18px;
            }
        `;
        document.head.appendChild(style);
    }

    const passwords = document.querySelectorAll('input[type="password"]');
    passwords.forEach((input) => {
        if (input.dataset.pwToggleAttached === 'true') return;
        input.dataset.pwToggleAttached = 'true';

        let wrapper = input.parentElement;
        const needsWrapper = !wrapper || getComputedStyle(wrapper).position !== 'relative';
        if (needsWrapper) {
            const newWrapper = document.createElement('div');
            newWrapper.className = 'pw-toggle-wrapper';
            input.parentNode.insertBefore(newWrapper, input);
            newWrapper.appendChild(input);
            wrapper = newWrapper;
        } else {
            wrapper.classList.add('pw-toggle-wrapper');
        }

        if (!wrapper.style.position || wrapper.style.position === 'static') {
            wrapper.style.position = 'relative';
        }

        const existingPaddingRight = parseInt(getComputedStyle(input).paddingRight || '0', 10);
        if (existingPaddingRight < 42) {
            const currentPr = input.style.paddingRight;
            input.style.paddingRight = currentPr || '42px';
        }

        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'pw-toggle-btn';
        btn.setAttribute('aria-label', 'Show password');
        btn.setAttribute('title', 'Show password');
        btn.tabIndex = -1;
        btn.innerHTML = `
            <svg class="pw-icon-eye" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"></path>
                <circle cx="12" cy="12" r="3"></circle>
            </svg>
            <svg class="pw-icon-eye-off" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" style="display:none;">
                <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"></path>
                <line x1="1" y1="1" x2="23" y2="23"></line>
            </svg>
        `;

        btn.addEventListener('click', (e) => {
            e.preventDefault();
            e.stopPropagation();
            const isPw = input.type === 'password';
            input.type = isPw ? 'text' : 'password';
            const eye = btn.querySelector('.pw-icon-eye');
            const eyeOff = btn.querySelector('.pw-icon-eye-off');
            if (isPw) {
                eye.style.display = 'none';
                eyeOff.style.display = 'block';
                btn.setAttribute('aria-label', 'Hide password');
                btn.setAttribute('title', 'Hide password');
            } else {
                eye.style.display = 'block';
                eyeOff.style.display = 'none';
                btn.setAttribute('aria-label', 'Show password');
                btn.setAttribute('title', 'Show password');
            }
        });

        wrapper.appendChild(btn);
    });
}

function clearLocalAuth() {
    localStorage.removeItem('token');
    localStorage.removeItem('user');
    localStorage.removeItem('adminToken');
}

function paintAuthUI(user) {
    const authLinks = document.getElementById('auth-links');
    const userInfo = document.getElementById('user-info');
    const usernameDisplay = document.getElementById('username-display');
    const heroBookingsBtn = document.getElementById('hero-bookings-btn');

    if (user) {
        if (authLinks) authLinks.classList.add('hidden');
        if (userInfo) userInfo.classList.remove('hidden');
        if (heroBookingsBtn) heroBookingsBtn.classList.remove('hidden');

        if (usernameDisplay) {
            const displayName = (user.full_name && user.full_name.trim())
                ? user.full_name
                : ((user.email && user.email.includes('@'))
                    ? user.email.split('@')[0]
                    : (user.username || 'User'));
            usernameDisplay.textContent = displayName;
        }
    } else {
        if (authLinks) authLinks.classList.remove('hidden');
        if (userInfo) userInfo.classList.add('hidden');
        if (heroBookingsBtn) heroBookingsBtn.classList.add('hidden');
        if (usernameDisplay) usernameDisplay.textContent = '';
    }
}

async function checkAuthStatus() {
    paintAuthUI(null);
    try {
        const res = await fetch('/api/auth/me', {
            method: 'GET',
            credentials: 'same-origin',
            headers: { 'Accept': 'application/json' }
        });
        const payload = await res.json();
        if (res.ok && payload && payload.success && payload.user) {
            if (payload.user.user_type === 'client') {
                localStorage.setItem('user', JSON.stringify(payload.user));
                paintAuthUI(payload.user);
                const token = localStorage.getItem('token');
                if (!token) localStorage.setItem('token', 'cookie-backed-session');
            } else {
                clearLocalAuth();
                paintAuthUI(null);
            }
        } else {
            clearLocalAuth();
            paintAuthUI(null);
        }
    } catch (e) {
        clearLocalAuth();
        paintAuthUI(null);
    }
}

async function loadPackages(sort = 'default') {
    const container = document.getElementById('packages-container');
    if (!container) return;
    
    try {
        // Cache buster to ensure fresh data
        const response = await fetch(`/packages?sort=${encodeURIComponent(sort)}&v=${Date.now()}`);
        const data = await response.json();
        
        if (data.success) {
            container.innerHTML = '';
            data.data.forEach(pkg => {
                const status = (pkg.availability_status || '').trim();
                const isAvailable = status === 'Available';
                const availableSpots = parseInt(pkg.available_spots || 0, 10);
                const statusText = isAvailable ? `${availableSpots} Seats Available` : '0 Seats Available';
                console.log(`Package: ${pkg.package_name}, Raw Status: "${pkg.availability_status}", Clean Status: "${status}", isAvailable: ${isAvailable}, available_spots: ${availableSpots}`);
                
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
                                ${statusText}
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
    clearLocalAuth();
    window.location.href = '/home';
}
