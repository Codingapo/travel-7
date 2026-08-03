let isLogin = true;

function toggleAuth() {
    isLogin = !isLogin;
    const loginForm = document.getElementById('login-form');
    const signupForm = document.getElementById('signup-form');
    const authTitle = document.getElementById('auth-title');

    if (isLogin) {
        loginForm.classList.remove('hidden');
        signupForm.classList.add('hidden');
        authTitle.textContent = 'Welcome back! Please login.';
    } else {
        loginForm.classList.add('hidden');
        signupForm.classList.remove('hidden');
        authTitle.textContent = 'Create your account to start booking.';
    }
    clearMessages();
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
                window.location.href = 'index.html';
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


document.getElementById('otp-form').addEventListener('submit', async (e)=>{

    e.preventDefault();


    const otp = document.getElementById('otp-code').value;


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
