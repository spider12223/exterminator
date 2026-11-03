local TEMPLATE = {
	level = 1,
	buffs = {},
}

local Pet = {}

function Pet.new(kind: string)
	local pet = table.clone(TEMPLATE)
	pet.kind = kind
	return pet
end

function Pet.addBuff(pet, buff: string)
	table.insert(pet.buffs, buff)
end

return Pet
