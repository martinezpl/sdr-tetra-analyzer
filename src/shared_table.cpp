#include "shared_table.h"

#include <cerrno>
#include <cstring>
#include <fcntl.h>
#include <stdexcept>
#include <string>
#include <sys/mman.h>
#include <unistd.h>

#ifndef MAP_ANON
#define MAP_ANON MAP_ANONYMOUS
#endif

struct SharedTable::State {
	struct SharedGrant {
		uint64_t sample;
		int usage_marker;
		uint32_t gssi;
		uint32_t issi;
		uint32_t control_hz;
		uint8_t status;
	};
	struct Lane {
		// 0 marks a free lane that no child follows yet.
		uint32_t hz;
		// One grant for each of the four timeslots of the carrier.
		SharedGrant timeslots[4];
	};

	// A bounded queue of the frequencies that some child wants a lane for.
	// The oldest entry goes when it overflows, the same rule as the GSSI cap.
	static const size_t DEMANDS = 32;

	size_t count;
	Demand demands[DEMANDS];
	size_t demand_head;
	size_t demand_count;
	Lane lanes[1];
};

SharedTable::SharedTable(State* state, size_t map_size, int lock_fd)
	: state(state), map_size(map_size), lock_fd(lock_fd)
{
}

SharedTable SharedTable::create(const std::vector<uint32_t>& carriers, size_t lanes)
{
	if (carriers.empty())
		throw std::runtime_error("the shared table needs at least one carrier");
	if (lanes < carriers.size()) lanes = carriers.size();
	char path[] = "/tmp/tetra-analyze-table.XXXXXX";
	int fd = mkstemp(path);
	if (fd < 0)
		throw std::runtime_error(std::string("mkstemp: ") + strerror(errno));
	unlink(path);
	size_t bytes = sizeof(State) + (lanes - 1) * sizeof(State::Lane);
	void* p = mmap(nullptr, bytes, PROT_READ | PROT_WRITE, MAP_SHARED | MAP_ANON, -1, 0);
	if (p == MAP_FAILED) {
		int e = errno;
		close(fd);
		throw std::runtime_error(std::string("mmap: ") + strerror(e));
	}
	State* s = static_cast<State*>(p);
	s->count = lanes;
	s->demand_head = 0;
	s->demand_count = 0;
	for (size_t i = 0; i < lanes; i++)
		s->lanes[i].hz = i < carriers.size() ? carriers[i] : 0;
	return SharedTable(s, bytes, fd);
}

SharedTable::SharedTable(SharedTable&& other) noexcept
	: state(other.state), map_size(other.map_size), lock_fd(other.lock_fd)
{
	other.state = nullptr;
	other.map_size = 0;
	other.lock_fd = -1;
}

SharedTable& SharedTable::operator=(SharedTable&& other) noexcept
{
	if (this == &other)
		return *this;
	if (state)
		munmap(state, map_size);
	if (lock_fd >= 0)
		close(lock_fd);
	state = other.state;
	map_size = other.map_size;
	lock_fd = other.lock_fd;
	other.state = nullptr;
	other.map_size = 0;
	other.lock_fd = -1;
	return *this;
}

SharedTable::~SharedTable()
{
	if (state)
		munmap(state, map_size);
	if (lock_fd >= 0)
		close(lock_fd);
}

bool SharedTable::set_lock(short type) const
{
	struct flock lock = {};
	lock.l_type = type;
	lock.l_whence = SEEK_SET;
	while (fcntl(lock_fd, F_SETLKW, &lock) < 0) {
		if (errno != EINTR)
			return false;
	}
	return true;
}

size_t SharedTable::lanes() const
{
	return state ? state->count : 0;
}

uint32_t SharedTable::assigned(size_t lane) const
{
	if (!state || lane >= state->count || !set_lock(F_RDLCK)) return 0;
	uint32_t hz = state->lanes[lane].hz;
	set_lock(F_UNLCK);
	return hz;
}

int SharedTable::assign(uint32_t hz)
{
	if (!state || !hz || !set_lock(F_WRLCK)) return -1;
	int lane = -1;
	for (size_t i = 0; i < state->count; i++) {
		// Another lane already follows it, so this is not a new carrier.
		if (state->lanes[i].hz == hz) { set_lock(F_UNLCK); return -1; }
		if (!state->lanes[i].hz && lane < 0) lane = (int)i;
	}
	if (lane >= 0) {
		State::Lane& taken = state->lanes[lane];
		taken.hz = hz;
		// The lane may hold grants of whatever it followed before it was freed.
		// It never is, today, but a stale grant would name the wrong talkgroup.
		for (unsigned tn = 0; tn < 4; tn++) taken.timeslots[tn] = State::SharedGrant{};
	}
	set_lock(F_UNLCK);
	return lane;
}

bool SharedTable::want(const Demand& d)
{
	if (!state || !d.hz || !set_lock(F_WRLCK)) return false;
	bool room = false;
	for (size_t i = 0; i < state->count; i++) {
		// Already followed, so there is nothing to ask for.
		if (state->lanes[i].hz == d.hz) { set_lock(F_UNLCK); return true; }
		if (!state->lanes[i].hz) room = true;
	}
	if (!room) { set_lock(F_UNLCK); return false; }
	for (size_t i = 0; i < state->demand_count; i++)
		if (state->demands[(state->demand_head + i) % State::DEMANDS].hz == d.hz) {
			set_lock(F_UNLCK);
			return true;
		}
	if (state->demand_count == State::DEMANDS) {
		state->demand_head = (state->demand_head + 1) % State::DEMANDS;
		state->demand_count--;
	}
	state->demands[(state->demand_head + state->demand_count) % State::DEMANDS] = d;
	state->demand_count++;
	set_lock(F_UNLCK);
	return true;
}

bool SharedTable::take(Demand* d)
{
	if (!state || !set_lock(F_WRLCK)) return false;
	bool got = state->demand_count > 0;
	if (got) {
		*d = state->demands[state->demand_head];
		state->demand_head = (state->demand_head + 1) % State::DEMANDS;
		state->demand_count--;
	}
	set_lock(F_UNLCK);
	return got;
}

bool SharedTable::publish(uint32_t carrier_hz, uint8_t tn_mask, int usage_marker, uint32_t gssi,
			  uint32_t issi, uint64_t sample, uint32_t control_hz)
{
	if (!state || !tn_mask || (tn_mask & 0xf0) || !set_lock(F_WRLCK))
		return false;
	bool found = false;
	for (size_t i = 0; i < state->count; i++) {
		if (!state->lanes[i].hz || state->lanes[i].hz != carrier_hz)
			continue;
		found = true;
		for (unsigned tn = 1; tn <= 4; tn++) {
			if (!(tn_mask & (8u >> (tn - 1))))
				continue;
			State::SharedGrant& grant = state->lanes[i].timeslots[tn - 1];
			if (!grant.status || sample > grant.sample) {
				grant.sample = sample;
				grant.usage_marker = usage_marker;
				grant.gssi = gssi;
				grant.issi = issi;
				grant.control_hz = control_hz;
				grant.status = 1;
			}
		}
		break;
	}
	set_lock(F_UNLCK);
	return found;
}

bool SharedTable::invalidate(uint32_t carrier_hz, uint8_t tn, uint64_t sample)
{
	if (!state || tn < 1 || tn > 4 || !set_lock(F_WRLCK))
		return false;
	bool found = false;
	for (size_t i = 0; i < state->count; i++) {
		if (state->lanes[i].hz != carrier_hz)
			continue;
		found = true;
		State::SharedGrant& grant = state->lanes[i].timeslots[tn - 1];
		if (!grant.status || sample >= grant.sample) {
			grant.sample = sample;
			grant.status = 2;
		}
		break;
	}
	set_lock(F_UNLCK);
	return found;
}

bool SharedTable::permits_clear(uint32_t carrier_hz, uint8_t tn, int local_encr, int dl_usage,
				uint64_t sample, Grant* out) const
{
	if (local_encr == 0)
		return true;
	if (!state || local_encr != -1 || tn < 1 || tn > 4 || dl_usage <= 3 ||
	    !set_lock(F_RDLCK))
		return false;
	bool permitted = false;
	for (size_t i = 0; i < state->count; i++) {
		if (state->lanes[i].hz != carrier_hz)
			continue;
		for (unsigned t = 0; t < 4; t++) {
			const State::SharedGrant& grant = state->lanes[i].timeslots[t];
			if (grant.status != 1 || grant.sample > sample ||
			    sample - grant.sample > 30 * 36000 ||
			    (grant.usage_marker >= 0 && grant.usage_marker != dl_usage))
				continue;
			permitted = true;
			if (out)
				*out = { grant.gssi, grant.issi, grant.control_hz };
			break;
		}
		break;
	}
	set_lock(F_UNLCK);
	return permitted;
}
