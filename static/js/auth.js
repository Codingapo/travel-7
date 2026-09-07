let isLogin = !window.location.pathname.endsWith("/register");
let otpCountdownTimer = null;
const OTP_RESEND_WAIT = 90;

function startOtpCountdown() {
    const resendBtn = document.getElementById('otp-resend-btn');
    const timerText = document.getElementById('timer-text');
    const timerIcon = document.getElementById('timer-icon');
    if (!resendBtn || !timerText || !timerIcon) return;

    if (otpCountdownTimer) clearInterval(otpCountdownTimer);
    let remaining = OTP_RESEND_WAIT;
    resendBtn.classList.add('hidden');
    timerIcon.innerHTML = '<i class="fas fa-hourglass-half"></i>';
    timerText.textContent = 'Resend available in 0:90';
    otpCountdownTimer = setInterval(() => {
        remaining--;
        if (remaining <= 0) {
            clearInterval(otpCountdownTimer);
            otpCountdownTimer = null;
            timerText.textContent = 'You can now resend a new code.';
            timerIcon.innerHTML = '<i class="fas fa-check-circle"></i>';
            resendBtn.classList.remove('hidden');
        } else {
            const mins = Math.floor(remaining / 60);
            const secs = remaining % 60;
            timerText.textContent = `Resend available in ${mins}:${secs < 10 ? '0' + secs : secs}`;
        }
    }, 1000);
}

function toggleAuth() {
    isLogin = !isLogin;
    const loginForm = document.getElementById('login-form');
    const signupForm = document.getElementById('signup-form');
    const authTitle = document.getElementById('auth-title');

    if (isLogin) {
        loginForm.classList.remove('hidden');
        signupForm.classList.add('hidden');
        authTitle.textContent = 'Welcome back! Please login.';
        history.replaceState({}, '', '/auth/login');
    } else {
        loginForm.classList.add('hidden');
        signupForm.classList.remove('hidden');
        authTitle.textContent = 'Create your account to start booking.';
        history.replaceState({}, '', '/auth/register');
    }
    clearMessages();
}

function applyAuthRoute() {
    const register = window.location.pathname.endsWith('/register');
    isLogin = !register;
    const loginForm = document.getElementById('login-form');
    const signupForm = document.getElementById('signup-form');
    const authTitle = document.getElementById('auth-title');
    if (!loginForm || !signupForm) return;
    loginForm.classList.toggle('hidden', !isLogin);
    signupForm.classList.toggle('hidden', isLogin);
    authTitle.textContent = isLogin ? 'Welcome back! Please login.' : 'Create your account to start booking.';
}

function clearMessages() {
    const errorEl = document.getElementById('auth-error');
    const successEl = document.getElementById('auth-success');
    if (errorEl) errorEl.classList.add('hidden');
    if (successEl) successEl.classList.add('hidden');
}

function showMessage(type, text) {
    const errorEl = document.getElementById('auth-error');
    const successEl = document.getElementById('auth-success');
    
    clearMessages();
    if (type === 'error') {
        errorEl.textContent = text;
        errorEl.classList.remove('hidden');
    } else {
        successEl.textContent = text;
        successEl.classList.remove('hidden');
    }
}

function validateEmail(email) {
    return String(email)
        .toLowerCase()
        .match(/^(([^<>()[\]\\.,;:\s@"]+(\.[^<>()[\]\\.,;:\s@"]+)*)|(".+"))@((\[[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\])|(([a-zA-Z\-0-9]+\.)+[a-zA-Z]{2,}))$/);
}

async function forgotPasswordFlow() {
    const email = prompt('Enter your account email or username:');
    if (!email) return;
    try {
        const req = await fetch('/api/auth/forgot-password/request', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ email, password: 'ignored' })
        });
        const payload = await req.json();
        if (!payload.success) {
            showMessage('error', payload.error || 'Unable to request OTP');
            return;
        }
        const otpFromServer = payload?.data?.otp || '';
        const otp = prompt(`Enter OTP sent to your account${otpFromServer ? ` (Dev OTP: ${otpFromServer})` : ''}:`);
        if (!otp) return;
        const newPassword = prompt('Enter your new password (min 6 chars):');
        if (!newPassword) return;
        const verify = await fetch('/api/auth/forgot-password/verify', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ email, otp, password: newPassword })
        });
        const verifyPayload = await verify.json();
        if (verifyPayload.success) {
            showMessage('success', 'Password reset successful. You can login now.');
        } else {
            showMessage('error', verifyPayload.error || 'OTP verification failed');
        }
    } catch (error) {
        showMessage('error', 'Password reset failed. Please try again.');
    }
}

// Login Handler
document.getElementById('login-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const email = document.getElementById('login-email').value.trim();
    const password = document.getElementById('login-password').value;

    if (!validateEmail(email)) {
        showMessage('error', 'Please enter a valid email address.');
        return;
    }

    if (password.length < 6) {
        showMessage('error', 'Password must be at least 6 characters.');
        return;
    }

    try {
        const response = await fetch('/api/auth/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ email, password })
        });
        const data = await response.json();

        if (data.success) {
            localStorage.setItem('token', data.token);
            localStorage.setItem('user', JSON.stringify(data.user));
            showMessage('success', 'Login successful! Redirecting...');
            setTimeout(() => {
                window.location.href = data.redirect || '/home';
            }, 1000);
        } else {
            showMessage('error', data.error);
        }
    } catch (error) {
        showMessage('error', 'An error occurred. Please try again.');
    }
});

// Signup Handler
let pendingEmail = "";


// Signup Handler
document.getElementById('signup-form').addEventListener('submit', async (e) => {
    e.preventDefault();

    const email = document.getElementById('signup-email').value.trim();
    const password = document.getElementById('signup-password').value;
    const confirm = document.getElementById('signup-confirm').value;


    if (!validateEmail(email)) {
        showMessage('error', 'Please enter a valid email address.');
        return;
    }


    if (password.length < 6) {
        showMessage('error', 'Password must be at least 6 characters.');
        return;
    }


    if (password !== confirm) {
        showMessage('error', 'Passwords do not match.');
        return;
    }


    try {

        const response = await fetch('/api/auth/register', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                email,
                password
            })
        });


        const data = await response.json();


if (data.success) {

            pendingEmail = email;

            document.getElementById('signup-form').classList.add('hidden');
            document.getElementById('otp-form').classList.remove('hidden');

            showMessage(
                'success',
                'Verification code sent. Check your email.'
            );

            startOtpCountdown();

        } else {

            showMessage(
                'error',
                data.error || data.detail
            );

        }


    } catch(error){

        showMessage(
            'error',
            'Unable to create account. Try again.'
        );

    }

});


// Resend registration OTP
document.getElementById('otp-resend-btn').addEventListener('click', async () => {
    const btn = document.getElementById('otp-resend-btn');
    if (!pendingEmail) return;
    btn.disabled = true;
    btn.textContent = 'Sending new code...';
    try {
        const resp = await fetch('/api/auth/register/resend', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ email: pendingEmail, password: '' })
        });
        const data = await resp.json();
        if (!data.success) {
            showMessage('error', data.error || 'Unable to resend verification code.');
            return;
        }
        document.getElementById('otp-code').value = '';
        document.getElementById('otp-code').focus();
        showMessage('success', 'A new verification code has been sent to your email.');
        startOtpCountdown();
    } catch (error) {
        showMessage('error', 'Unable to resend the verification code.');
    } finally {
        btn.disabled = false;
        btn.textContent = "Didn't receive the code? Send again";
    }
});


document.getElementById('otp-form').addEventListener('submit', async (e)=>{

    e.preventDefault();


const otp = document.getElementById('otp-code').value.trim();
    try {

        const response = await fetch('/api/auth/verify-registration',{

            method:'POST',

            headers:{
                'Content-Type':'application/json'
            },

            body:JSON.stringify({

                email: pendingEmail,

                otp

            })

        });


        const data = await response.json();


        if(data.success){

            showMessage(
                'success',
                'Account verified. You can now login.'
            );


            setTimeout(()=>{

                toggleAuth();

            },1500);


        }else{

            showMessage(
                'error',
                data.error || data.detail
            );

        }


    }catch(error){

        showMessage(
            'error',
            'Verification failed. Try again.'
        );

    }

});


document.addEventListener("DOMContentLoaded", applyAuthRoute);
