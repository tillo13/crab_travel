-- =============================================================
-- crab.travel MVP — Database Schema
-- Run against kumori-404602 Cloud SQL instance
-- =============================================================

CREATE SCHEMA IF NOT EXISTS crab;

-- Users (authenticated via Google OAuth)
-- This is the persistent identity — preferences compound over time
CREATE TABLE IF NOT EXISTS crab.users (
    pk_id SERIAL PRIMARY KEY,
    google_id VARCHAR(255) UNIQUE NOT NULL,
    email VARCHAR(255) NOT NULL,
    full_name VARCHAR(255),
    picture_url TEXT,
    home_location VARCHAR(500),
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- User Profiles (persistent preferences — the core data asset)
-- One per user. Gets smarter over time. Shared across all plans.
CREATE TABLE IF NOT EXISTS crab.user_profiles (
    pk_id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL UNIQUE REFERENCES crab.users(pk_id) ON DELETE CASCADE,
    interests JSONB DEFAULT '[]',
    dietary_needs TEXT,
    mobility_notes TEXT,
    travel_style VARCHAR(50),
    accommodation_preference VARCHAR(50),
    budget_comfort VARCHAR(20),
    bio TEXT,
    completed BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- Plans (generalized — trips, monthly plans, events, etc.)
CREATE TABLE IF NOT EXISTS crab.plans (
    pk_id SERIAL PRIMARY KEY,
    plan_id UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    organizer_id INTEGER NOT NULL REFERENCES crab.users(pk_id),
    plan_type VARCHAR(30) NOT NULL DEFAULT 'trip',
    title VARCHAR(255) NOT NULL,
    destination VARCHAR(500),
    start_date DATE,
    end_date DATE,
    headcount INTEGER,
    description TEXT,
    invite_token VARCHAR(64) UNIQUE NOT NULL,
    status VARCHAR(20) DEFAULT 'planning',
    recurring BOOLEAN DEFAULT FALSE,
    travel_window_start DATE,
    travel_window_end DATE,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_plans_organizer ON crab.plans(organizer_id);
CREATE INDEX IF NOT EXISTS idx_plans_invite_token ON crab.plans(invite_token);
CREATE INDEX IF NOT EXISTS idx_plans_type ON crab.plans(plan_type);

-- Plan Members (both authed users AND anonymous invitees)
CREATE TABLE IF NOT EXISTS crab.plan_members (
    pk_id SERIAL PRIMARY KEY,
    plan_id UUID NOT NULL REFERENCES crab.plans(plan_id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES crab.users(pk_id),
    display_name VARCHAR(255) NOT NULL,
    email VARCHAR(255),
    member_token VARCHAR(64) UNIQUE NOT NULL,
    role VARCHAR(20) DEFAULT 'member',
    home_airport VARCHAR(10),
    is_flexible BOOLEAN DEFAULT FALSE,
    joined_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(plan_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_plan_members_plan ON crab.plan_members(plan_id);
CREATE INDEX IF NOT EXISTS idx_plan_members_token ON crab.plan_members(member_token);

-- Plan-Specific Preferences (overrides/supplements the user profile for this plan)
-- Anonymous members who have no user_profile get a full set here
-- Authed members may only need budget + room pref (rest comes from profile)
CREATE TABLE IF NOT EXISTS crab.plan_preferences (
    pk_id SERIAL PRIMARY KEY,
    member_id INTEGER NOT NULL UNIQUE REFERENCES crab.plan_members(pk_id) ON DELETE CASCADE,
    budget_min INTEGER,
    budget_max INTEGER,
    accommodation_style VARCHAR(50),
    dietary_needs TEXT,
    interests JSONB DEFAULT '[]',
    mobility_notes TEXT,
    room_preference VARCHAR(50),
    notes TEXT,
    completed BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- Recommendations (Claude-generated, tied to a plan)
CREATE TABLE IF NOT EXISTS crab.recommendations (
    pk_id SERIAL PRIMARY KEY,
    recommendation_id UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    plan_id UUID NOT NULL REFERENCES crab.plans(plan_id) ON DELETE CASCADE,
    category VARCHAR(50) NOT NULL,
    title VARCHAR(500) NOT NULL,
    description TEXT,
    price_estimate VARCHAR(100),
    url TEXT,
    compatibility_score INTEGER,
    ai_reasoning TEXT,
    metadata JSONB DEFAULT '{}',
    status VARCHAR(20) DEFAULT 'suggested',
    generated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_recommendations_plan ON crab.recommendations(plan_id);

-- Itinerary Items
CREATE TABLE IF NOT EXISTS crab.itinerary_items (
    pk_id SERIAL PRIMARY KEY,
    item_id UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    plan_id UUID NOT NULL REFERENCES crab.plans(plan_id) ON DELETE CASCADE,
    recommendation_id UUID REFERENCES crab.recommendations(recommendation_id),
    title VARCHAR(500) NOT NULL,
    category VARCHAR(50),
    scheduled_date DATE,
    scheduled_time TIME,
    duration_minutes INTEGER,
    location VARCHAR(500),
    url TEXT,
    notes TEXT,
    status VARCHAR(20) DEFAULT 'pending',
    added_by INTEGER REFERENCES crab.plan_members(pk_id),
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_itinerary_plan ON crab.itinerary_items(plan_id);
CREATE INDEX IF NOT EXISTS idx_itinerary_date ON crab.itinerary_items(plan_id, scheduled_date);

-- Expenses
CREATE TABLE IF NOT EXISTS crab.expenses (
    pk_id SERIAL PRIMARY KEY,
    expense_id UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    plan_id UUID NOT NULL REFERENCES crab.plans(plan_id) ON DELETE CASCADE,
    paid_by INTEGER NOT NULL REFERENCES crab.plan_members(pk_id),
    title VARCHAR(255) NOT NULL,
    amount INTEGER NOT NULL,
    category VARCHAR(50),
    split_type VARCHAR(20) DEFAULT 'equal',
    split_among JSONB DEFAULT '[]',
    receipt_url TEXT,
    expense_date DATE DEFAULT CURRENT_DATE,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_expenses_plan ON crab.expenses(plan_id);

-- Member Blackouts (dates people CANNOT travel)
CREATE TABLE IF NOT EXISTS crab.member_blackouts (
    pk_id SERIAL PRIMARY KEY,
    plan_id UUID NOT NULL REFERENCES crab.plans(plan_id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES crab.users(pk_id),
    blackout_start DATE NOT NULL,
    blackout_end DATE NOT NULL,
    UNIQUE(plan_id, user_id, blackout_start, blackout_end)
);

-- AI Usage Tracking
CREATE TABLE IF NOT EXISTS crab.ai_usage (
    pk_id SERIAL PRIMARY KEY,
    plan_id UUID REFERENCES crab.plans(plan_id),
    user_id INTEGER REFERENCES crab.users(pk_id),
    model VARCHAR(100),
    input_tokens INTEGER,
    output_tokens INTEGER,
    cost_cents INTEGER,
    feature VARCHAR(50),
    created_at TIMESTAMP DEFAULT NOW()
);
