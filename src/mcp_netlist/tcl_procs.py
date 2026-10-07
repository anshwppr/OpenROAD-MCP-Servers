"""Tcl helper procs for the netlist server, added to the shared OpenROAD driver.

They are hardened versions of the procs in ``openroad_bundle/orhelp.tcl``: exact name
lookups, structured ``__mcp_rec`` output (one record per item), set_disable_timing that is
always undone, and no silent ``catch`` around timing queries.

Semantics: depth = number of gates (unit-delay library, arrival == gate count; register
starts include CLK->Q = 1, subtracted on the Python side); flip-flops bound every cone and
path; netlist constants are neutralized with set_logic_dc at load so gates fed by 1'b0/1'b1
are traversed structurally instead of being blocked by constant propagation.
"""

NETLIST_DRIVER_TCL = r"""
proc __nl_null {obj} {
  expr {$obj eq "" || $obj eq "NULL"}
}

proc __nl_block {} {
  set block [ord::get_db_block]
  if {[__nl_null $block]} { error "No design is loaded. Call load_design first." }
  return $block
}

# Design-wide lookup tables, filled by __nl_init after link_design.
set ::__nl_seq [dict create]
set ::__nl_clk [dict create]
set ::__nl_pi [dict create]
set ::__nl_po [dict create]
set ::__nl_const [dict create]

# Constant nets: OpenROAD names 1'b1 / 1'b0 nets one_ / zero_ (only if nothing drives them).
proc __nl_find_constants {} {
  set out [dict create]
  set block [__nl_block]
  foreach {name value} {one_ 1 zero_ 0} {
    set net [$block findNet $name]
    if {[__nl_null $net] || [llength [$net getBTerms]]} continue
    set driven 0
    foreach it [$net getITerms] {
      if {[$it getIoType] eq "OUTPUT"} { set driven 1 }
    }
    if {!$driven} { dict set out $name $value }
  }
  return $out
}

proc __nl_init {neutralize} {
  set ::__nl_seq [dict create]
  set ::__nl_clk [dict create]
  set ::__nl_pi [dict create]
  set ::__nl_po [dict create]
  foreach c [all_registers -cells] { dict set ::__nl_seq [get_property $c ref_name] 1 }
  foreach p [all_registers -clock_pins] { dict set ::__nl_clk [get_property $p lib_pin_name] 1 }
  foreach p [all_inputs] { dict set ::__nl_pi [get_full_name $p] 1 }
  foreach p [all_outputs] { dict set ::__nl_po [get_full_name $p] 1 }
  set ::__nl_const [__nl_find_constants]
  set pins {}
  if {$neutralize && [dict size $::__nl_const]} {
    set pins [get_pins -quiet -of_objects [get_nets -quiet [dict keys $::__nl_const]]]
    if {[llength $pins]} {
      # "propagated logic value differs from constraint value" is expected (reported lazily).
      catch {suppress_message STA 1521}
      set_logic_dc $pins
    }
  }
  # Levelize and time the whole graph now. If the first timing update happens while a cell
  # is disabled (path_exists with avoid), unset_disable_timing leaves stale arrivals behind.
  sta::find_timing -full_update
  __mcp_rec neutralized [llength $pins]
}

proc __nl_const_text {netname} {
  if {[dict exists $::__nl_const $netname]} {
    return [expr {[dict get $::__nl_const $netname] ? "1'b1" : "1'b0"}]
  }
  return $netname
}

# ------------------------------------------------------------------ names

# STA objects whose full name is exactly $name (names are literal, never patterns).
proc __nl_exact {cmd name} {
  set out {}
  foreach o [$cmd -quiet $name] {
    if {[get_full_name $o] eq $name} { lappend out $o }
  }
  return $out
}

proc __nl_missing {name} {
  set bits [get_ports -quiet $name]
  if {[llength $bits] > 1} {
    set names [lsort -dictionary [lmap b $bits {get_full_name $b}]]
    error "'$name' is a [llength $bits]-bit port; name a single bit, one of [lindex $names 0] .. [lindex $names end]."
  }
  error "No port, net or gate named '$name' in the design."
}

# {kind object}: kind is input / output (port), net, or instance.
proc __nl_lookup {name} {
  set p [__nl_exact get_ports $name]
  if {[llength $p]} { return [list [get_property [lindex $p 0] direction] [lindex $p 0]] }
  set n [__nl_exact get_nets $name]
  if {[llength $n]} { return [list net [lindex $n 0]] }
  set c [__nl_exact get_cells $name]
  if {[llength $c]} { return [list instance [lindex $c 0]] }
  __nl_missing $name
}

proc __nl_driver_pin {net} {
  lindex [get_pins -quiet -of_objects $net -filter "direction==output"] 0
}

# Timing node for a name: a port, or the output pin driving a net / of a gate.
# role "src" (forward searches) turns an output port into the pin that drives it.
proc __nl_node {name role} {
  lassign [__nl_lookup $name] kind obj
  switch $kind {
    input { return [list input $obj] }
    output {
      if {$role eq "sink"} { return [list output $obj] }
      set drv [__nl_driver_pin [get_nets -quiet $name]]
      if {$drv eq ""} { error "Output '$name' is not driven by any gate." }
      return [list net $drv]
    }
    net {
      set drv [__nl_driver_pin $obj]
      if {$drv eq ""} { error "Net '$name' is not driven by any gate or input." }
      return [list net $drv]
    }
    instance {
      set out [lindex [get_pins -quiet -of_objects $obj -filter "direction==output"] 0]
      if {$out eq ""} { error "Gate '$name' has no output pin." }
      return [list instance $out]
    }
  }
}

proc __nl_is_seq {cell} {
  dict exists $::__nl_seq [get_property $cell ref_name]
}

# One record per cell: tag name ref_name is_seq (the top instance, with an empty name, is dropped).
proc __nl_cells {tag cells} {
  foreach c $cells {
    set n [get_full_name $c]
    if {$n eq ""} continue
    set r [get_property $c ref_name]
    __mcp_rec $tag $n $r [dict exists $::__nl_seq $r]
  }
}

# ------------------------------------------------------------------ cones

proc __nl_fanin_cone {name} {
  lassign [__nl_node $name sink] kind obj
  __mcp_rec node $kind [get_full_name $obj]
  __nl_cells cell [get_fanin -to $obj -flat -only_cells]
  foreach p [get_fanin -to $obj -flat -startpoints_only] {
    set n [get_full_name $p]
    if {[dict exists $::__nl_pi $n]} { __mcp_rec start $n }
  }
}

proc __nl_fanout_cone {name} {
  lassign [__nl_node $name src] kind obj
  set drv ""
  if {$kind ne "input"} { set drv [get_full_name [get_cells -of_objects $obj]] }
  __mcp_rec node $kind [get_full_name $obj] $drv
  __nl_cells cell [get_fanout -from $obj -flat -only_cells]
  foreach e [get_fanout -from $obj -flat -endpoints_only] {
    set n [get_full_name $e]
    if {[dict exists $::__nl_po $n]} { __mcp_rec po $n }
  }
}

# Combinational gate count of each output's fanin cone.
proc __nl_output_cones {} {
  foreach o [all_outputs] {
    set k 0
    foreach c [get_fanin -to $o -flat -only_cells] {
      if {[get_full_name $c] ne "" && ![__nl_is_seq $c]} { incr k }
    }
    __mcp_rec cone [get_full_name $o] $k
  }
}

# Names of primary outputs reachable from obj through enabled arcs (honours set_disable_timing).
proc __nl_po_reach {obj} {
  set r {}
  foreach e [get_fanout -from $obj -flat -endpoints_only -trace_arcs enabled] {
    set n [get_full_name $e]
    if {[dict exists $::__nl_po $n]} { lappend r $n }
  }
  return $r
}

# ------------------------------------------------------------------ timing paths

# Run script with timing disabled through the given cells; always re-enable them.
proc __nl_with_disabled {objs script} {
  foreach o $objs { set_disable_timing $o }
  set rc [catch {uplevel 1 $script} res opts]
  foreach o $objs { catch {unset_disable_timing $o} }
  return -options $opts $res
}

# Path searches between two names as {tag find_timing_paths-args} specs. Run them one at a
# time and read each result at once: every find_timing_paths call frees the previous paths.
# Without $from: tag "pi" (from primary inputs) and tag "reg" (from register clock pins; its
# arrival includes CLK->Q). Sets ::__nl_meta {sink_kind sink_pin from_kind from_pin}.
proc __nl_specs {to from} {
  lassign [__nl_node $to sink] tkind tobj
  set ::__nl_meta [list $tkind [get_full_name $tobj] "" ""]
  if {$tkind eq "input"} { return {} }
  set target [expr {$tkind eq "output" ? [list -to $tobj] : [list -through $tobj]}]
  if {$from ne ""} {
    lassign [__nl_node $from src] fkind fobj
    lset ::__nl_meta 2 $fkind
    lset ::__nl_meta 3 [get_full_name $fobj]
    if {$fkind eq "input"} { return [list [list from [list -from $fobj {*}$target]]] }
    set target [list -through $fobj {*}$target]
  }
  set specs {}
  foreach {tag starts} [list pi [all_inputs] reg [all_registers -clock_pins]] {
    if {[llength $starts]} { lappend specs [list $tag [list -from $starts {*}$target]] }
  }
  return $specs
}

proc __nl_find {spec} {
  find_timing_paths -unconstrained {*}[lindex $spec 1] -group_path_count 1
}

# 1 if any of the searches finds a path.
proc __nl_any_path {specs} {
  foreach spec $specs {
    if {[llength [__nl_find $spec]]} { return 1 }
  }
  return 0
}

# Gate whose output is this path point ("" for ports and input pins).
proc __nl_pin_gate {pin} {
  set name [get_full_name $pin]
  if {[dict exists $::__nl_po $name] || [dict exists $::__nl_pi $name]} { return "" }
  if {[get_property $pin direction] ne "output"} { return "" }
  set c [get_cells -quiet -of_objects $pin]
  if {![llength $c]} { return "" }
  return [get_full_name $c]
}

# Records: path tag idx start end arrival; pt tag idx pin arrival gate.
proc __nl_emit_paths {tag paths} {
  set i 0
  foreach p $paths {
    set pts [get_property $p points]
    __mcp_rec path $tag $i [get_full_name [get_property [lindex $pts 0] pin]] \
      [get_full_name [get_property [lindex $pts end] pin]] [get_property [lindex $pts end] arrival]
    foreach pt $pts {
      set pin [get_property $pt pin]
      __mcp_rec pt $tag $i [get_full_name $pin] [get_property $pt arrival] [__nl_pin_gate $pin]
    }
    incr i
  }
}

proc __nl_emit_specs {specs} {
  foreach spec $specs { __nl_emit_paths [lindex $spec 0] [__nl_find $spec] }
}

proc __nl_emit_meta {} {
  __mcp_rec meta {*}$::__nl_meta
}

proc __nl_depth {to from} {
  set specs [__nl_specs $to $from]
  __nl_emit_meta
  __nl_emit_specs $specs
}

# Endpoint-only paths (no points) for a group: path tag idx start end arrival.
proc __nl_endpoint_paths {tag starts ends count} {
  if {![llength $starts] || ![llength $ends]} return
  set i 0
  foreach p [find_timing_paths -unconstrained -from $starts -to $ends -group_path_count $count -endpoint_path_count 1] {
    set pts [get_property $p points]
    __mcp_rec path $tag $i [get_full_name [get_property [lindex $pts 0] pin]] \
      [get_full_name [get_property [lindex $pts end] pin]] [get_property [lindex $pts end] arrival]
    incr i
  }
}

# Worst path of each start/end group (with points).
proc __nl_depth_summary {through} {
  set extra {}
  if {$through ne ""} {
    lassign [__nl_node $through src] kind pin
    set extra [list -through $pin]
    __mcp_rec through [get_full_name $pin]
  }
  set pi [all_inputs]
  set ck [all_registers -clock_pins]
  set po [all_outputs]
  set d [all_registers -data_pins]
  foreach {tag starts ends} [list pi_to_po $pi $po pi_to_reg $pi $d reg_to_reg $ck $d reg_to_po $ck $po] {
    if {![llength $starts] || ![llength $ends]} continue
    __nl_emit_paths $tag [find_timing_paths -unconstrained -from $starts {*}$extra -to $ends -group_path_count 1]
  }
}

# 1 if the sink is structurally reachable from the source through enabled timing arcs
# (honours set_disable_timing, stops at flip-flops). Starts at the source itself, so gates
# upstream of an internal source do not matter.
proc __nl_reaches {fobj tobj} {
  set tname [get_full_name $tobj]
  if {[get_full_name $fobj] eq $tname} { return 1 }
  foreach p [get_fanout -from $fobj -flat -trace_arcs enabled] {
    if {[get_full_name $p] eq $tname} { return 1 }
  }
  return 0
}

# Cells to disable for "avoid" names: a gate, or the gate driving a net / output port.
# The source's own driver is not on any path that starts at the source, so it is skipped.
proc __nl_avoid_cells {avoid src_kind src_obj} {
  set src_name [get_full_name $src_obj]
  set src_drv [expr {$src_kind eq "input" ? "" : [get_full_name [get_cells -of_objects $src_obj]]}]
  set cells {}
  foreach a $avoid {
    lassign [__nl_lookup $a] kind obj
    set c ""
    switch $kind {
      instance { set c $obj }
      input {
        __mcp_rec avoid $a input [get_full_name $obj] [expr {[get_full_name $obj] eq $src_name}]
        continue
      }
      default {
        set drv [__nl_driver_pin [expr {$kind eq "net" ? $obj : [get_nets -quiet $a]}]]
        if {$drv eq ""} {
          __mcp_rec avoid $a undriven ""
          continue
        }
        set c [get_cells -of_objects $drv]
      }
    }
    if {[get_full_name $c] eq $src_drv} {
      __mcp_rec avoid $a source_driver $src_drv
      continue
    }
    lappend cells $c
    __mcp_rec avoid $a gate [get_full_name $c]
  }
  return $cells
}

proc __nl_path_exists {from to avoid} {
  lassign [__nl_node $from src] fkind fobj
  lassign [__nl_node $to sink] tkind tobj
  set cells [__nl_avoid_cells $avoid $fkind $fobj]
  __nl_with_disabled $cells {
    __mcp_rec reach [__nl_reaches $fobj $tobj]
    set specs [__nl_specs $to $from]
    __nl_emit_meta
    __nl_emit_specs $specs
  }
}

# {found gate-names}: combinational gates on the first path found, strictly after the
# source and up to the sink.
proc __nl_path_gates {specs} {
  lassign $::__nl_meta tkind tpin fkind fpin
  foreach spec $specs {
    set paths [__nl_find $spec]
    if {![llength $paths]} continue
    set gates {}
    set started [expr {$fpin eq "" || $fkind eq "input"}]
    set first 1
    foreach pt [get_property [lindex $paths 0] points] {
      set pin [get_property $pt pin]
      set pname [get_full_name $pin]
      if {$first} {
        set first 0
        if {$started} continue
      }
      if {!$started} {
        if {$pname eq $fpin} { set started 1 }
        continue
      }
      set gname [__nl_pin_gate $pin]
      if {$gname ne "" && ![__nl_is_seq [get_cells $gname]]} { lappend gates $gname }
      if {$pname eq $tpin} break
    }
    return [list 1 $gates]
  }
  return [list 0 {}]
}

# Gates whose removal disconnects from -> to. Candidates are the gates of one path (an
# articulation point lies on every path); each is checked structurally with the gate
# disabled. With only_gate, test just that gate.
proc __nl_artic {from to only_gate} {
  lassign [__nl_node $from src] fkind fobj
  lassign [__nl_node $to sink] tkind tobj
  set specs [__nl_specs $to $from]
  __nl_emit_meta
  if {![__nl_reaches $fobj $tobj]} {
    __mcp_rec nopath
    return
  }
  if {$only_gate ne ""} {
    lassign [__nl_lookup $only_gate] kind obj
    if {$kind ne "instance"} {
      set drv [__nl_driver_pin [expr {$kind eq "net" ? $obj : [get_nets -quiet $only_gate]}]]
      if {$drv eq ""} { error "'$only_gate' is not a gate and has no driving gate." }
      set obj [get_cells -of_objects $drv]
    }
    set gates [list [get_full_name $obj]]
  } else {
    lassign [__nl_path_gates $specs] found gates
    if {!$found} {
      # No timed path (the source has no arrival): fall back to the cone intersection.
      set fanout [dict create]
      foreach c [get_fanout -from $fobj -flat -only_cells] { dict set fanout [get_full_name $c] 1 }
      set gates {}
      foreach c [get_fanin -to $tobj -flat -only_cells] {
        set n [get_full_name $c]
        if {$n ne "" && [dict exists $fanout $n] && ![__nl_is_seq $c]} { lappend gates $n }
      }
    }
  }
  foreach g $gates {
    set blocked [__nl_with_disabled [list [get_cells $g]] { expr {![__nl_reaches $fobj $tobj]} }]
    __mcp_rec cand $g $blocked
  }
}

# Pairs pi->po that lose every path when the wire's driver is blocked.
proc __nl_cut {wire stop_first budget_ms} {
  set t0 [clock milliseconds]
  lassign [__nl_node $wire sink] kind obj
  set wname [get_full_name $obj]
  if {$kind eq "input"} {
    foreach q [__nl_po_reach $obj] {
      if {$q ne $wname} { __mcp_rec pair $wname $q }
    }
    __mcp_rec done 1 1 1
    return
  }
  set drvpin [expr {$kind eq "output" ? [__nl_driver_pin [get_nets -quiet $wname]] : $obj}]
  if {$drvpin eq ""} {
    __mcp_rec done 0 0 1
    return
  }
  set drv [get_cells -of_objects $drvpin]
  set pis {}
  foreach p [get_fanin -to $drvpin -flat -startpoints_only] {
    if {[dict exists $::__nl_pi [get_full_name $p]]} { lappend pis $p }
  }
  set pis [lsort -dictionary -unique $pis]
  set before [dict create]
  set complete 1
  foreach p $pis {
    dict set before [get_full_name $p] [__nl_po_reach $p]
    if {[clock milliseconds] - $t0 > $budget_ms} { set complete 0; break }
  }
  set checked 0
  set found 0
  if {$complete} {
    __nl_with_disabled [list $drv] {
      foreach p $pis {
        set pn [get_full_name $p]
        set after [dict create]
        foreach q [__nl_po_reach $p] { dict set after $q 1 }
        foreach q [dict get $before $pn] {
          if {$q ne $wname && ![dict exists $after $q]} {
            __mcp_rec pair $pn $q
            incr found
          }
        }
        incr checked
        if {$stop_first && $found} break
        if {[clock milliseconds] - $t0 > $budget_ms} { set complete 0; break }
      }
    }
  }
  __mcp_rec done $checked [llength $pis] $complete
}

# ------------------------------------------------------------------ OpenDB connectivity

proc __nl_inst {name} {
  set inst [[__nl_block] findInst $name]
  if {[__nl_null $inst]} { __nl_missing $name }
  return $inst
}

proc __nl_net_name {net} {
  if {[__nl_null $net]} { return "" }
  return [$net getName]
}

# Records for every pin and port on a net: tag net io gate pin master; ${tag}port net io port.
proc __nl_net_conn {tag net} {
  set name [$net getName]
  foreach it [$net getITerms] {
    set inst [$it getInst]
    __mcp_rec $tag $name [string tolower [$it getIoType]] [$inst getName] [[$it getMTerm] getName] \
      [[$inst getMaster] getName]
  }
  foreach bt [$net getBTerms] {
    __mcp_rec ${tag}port $name [string tolower [$bt getIoType]] [$bt getName]
  }
}

# Gate or net named $name -> {kind obj}; kind is instance or net (port names map to their net).
proc __nl_odb_object {name kind} {
  set block [__nl_block]
  set inst [$block findInst $name]
  set net [$block findNet $name]
  if {[__nl_null $net]} {
    set bt [$block findBTerm $name]
    if {![__nl_null $bt]} { set net [$bt getNet] }
  }
  set has_inst [expr {![__nl_null $inst]}]
  set has_net [expr {![__nl_null $net]}]
  switch $kind {
    gate - instance {
      if {!$has_inst} { error "No gate named '$name'." }
      return [list instance $inst]
    }
    net {
      if {!$has_net} { __nl_missing $name }
      return [list net $net]
    }
  }
  if {$has_inst && $has_net} { error "'$name' is both a gate and a net; pass kind=\"gate\" or kind=\"net\"." }
  if {$has_inst} { return [list instance $inst] }
  if {$has_net} { return [list net $net] }
  __nl_missing $name
}

proc __nl_fanout {name kind} {
  lassign [__nl_odb_object $name $kind] k obj
  if {$k eq "instance"} {
    __mcp_rec source gate [$obj getName] [[$obj getMaster] getName]
    foreach it [$obj getITerms] {
      if {[$it getIoType] eq "OUTPUT" && ![__nl_null [$it getNet]]} { __nl_net_conn conn [$it getNet] }
    }
  } else {
    __mcp_rec source net [$obj getName] ""
    __nl_net_conn conn $obj
  }
}

# Pins of one gate: pin pin io net constant(1'b0/1'b1 or "") plus the driver / loads of each net.
proc __nl_gate_info {name} {
  set inst [__nl_inst $name]
  __mcp_rec gate [$inst getName] [[$inst getMaster] getName]
  foreach it [$inst getITerms] {
    set net [$it getNet]
    set nname [__nl_net_name $net]
    set const [expr {[dict exists $::__nl_const $nname] ? [__nl_const_text $nname] : ""}]
    __mcp_rec pin [[$it getMTerm] getName] [string tolower [$it getIoType]] $nname $const
    if {$nname eq "" || $const ne ""} continue
    if {[$it getIoType] eq "INPUT"} {
      foreach d [$net getITerms] {
        if {[$d getIoType] eq "OUTPUT"} { __mcp_rec drv [[$it getMTerm] getName] [[$d getInst] getName] [[[$d getInst] getMaster] getName] }
      }
      foreach b [$net getBTerms] {
        if {[$b getIoType] eq "INPUT"} { __mcp_rec drv [[$it getMTerm] getName] [$b getName] port }
      }
    } else {
      set k 0
      foreach l [$net getITerms] { if {[$l getIoType] eq "INPUT"} { incr k } }
      __mcp_rec loads [[$it getMTerm] getName] $k [llength [$net getBTerms]]
    }
  }
}

proc __nl_gate_counts {} {
  set counts [dict create]
  foreach inst [[__nl_block] getInsts] { dict incr counts [[$inst getMaster] getName] }
  foreach lib [[ord::get_db] getLibs] {
    foreach m [$lib getMasters] { __mcp_rec master [$m getName] }
  }
  dict for {m n} $counts { __mcp_rec count $m $n }
  dict for {m _} $::__nl_seq { __mcp_rec seq $m }
}

proc __nl_ports {} {
  foreach bt [[__nl_block] getBTerms] {
    __mcp_rec port [$bt getName] [string tolower [$bt getIoType]]
  }
}

proc __nl_list_gates {type pattern clock include_pins} {
  foreach inst [[__nl_block] getInsts] {
    set m [[$inst getMaster] getName]
    if {$type ne "" && $m ne $type} continue
    if {![string match $pattern [$inst getName]]} continue
    set pins {}
    set on_clock [expr {$clock eq ""}]
    foreach it [$inst getITerms] {
      set pin [[$it getMTerm] getName]
      set nname [__nl_net_name [$it getNet]]
      if {!$on_clock && [dict exists $::__nl_clk $pin] && $nname eq $clock} { set on_clock 1 }
      if {$include_pins} { lappend pins "$pin=[__nl_const_text $nname]" }
    }
    if {!$on_clock} continue
    __mcp_rec gate [$inst getName] $m {*}$pins
  }
}

proc __nl_constant_inputs {value type} {
  dict for {name v} $::__nl_const {
    if {$value ne "any" && $v != $value} continue
    set net [[__nl_block] findNet $name]
    foreach it [$net getITerms] {
      if {[$it getIoType] ne "INPUT"} continue
      set inst [$it getInst]
      set m [[$inst getMaster] getName]
      if {$type ne "" && $m ne $type} continue
      __mcp_rec cload [$inst getName] $m [[$it getMTerm] getName] [__nl_const_text $name]
    }
  }
  dict for {name v} $::__nl_const {
    __mcp_rec cnet $name [__nl_const_text $name] [llength [[[__nl_block] findNet $name] getITerms]]
  }
}

proc __nl_floating {} {
  set block [__nl_block]
  foreach bt [$block getBTerms] {
    set net [$bt getNet]
    set name [$bt getName]
    if {[__nl_null $net]} {
      __mcp_rec [expr {[$bt getIoType] eq "INPUT" ? "unused_input" : "undriven_output"}] $name
      continue
    }
    set loads 0
    set drivers 0
    foreach it [$net getITerms] {
      if {[$it getIoType] eq "INPUT"} { incr loads } else { incr drivers }
    }
    foreach other [$net getBTerms] {
      if {$other eq $bt} continue
      if {[$other getIoType] eq "OUTPUT"} { incr loads } else { incr drivers }
    }
    if {[$bt getIoType] eq "INPUT" && !$loads} { __mcp_rec unused_input $name }
    if {[$bt getIoType] eq "OUTPUT" && !$drivers} { __mcp_rec undriven_output $name }
  }
  foreach net [$block getNets] {
    set name [$net getName]
    if {[dict exists $::__nl_const $name] || [llength [$net getBTerms]]} continue
    set loads 0
    set drivers {}
    foreach it [$net getITerms] {
      if {[$it getIoType] eq "INPUT"} { incr loads } else { lappend drivers [[$it getInst] getName] }
    }
    if {[llength $drivers] && !$loads} { __mcp_rec dangling $name [lindex $drivers 0] }
    if {![llength $drivers] && $loads} { __mcp_rec undriven_net $name $loads }
  }
  foreach inst [$block getInsts] {
    foreach it [$inst getITerms] {
      if {[__nl_null [$it getNet]]} { __mcp_rec unconnected [$inst getName] [[$it getMTerm] getName] }
    }
  }
}

# Load-pin count per net (primary-input nets only for scope "inputs"); emits the top rows and all ties.
proc __nl_fanout_ranking {scope top_n include_constants} {
  set rows {}
  foreach net [[__nl_block] getNets] {
    set name [$net getName]
    if {!$include_constants && [dict exists $::__nl_const $name]} continue
    if {$scope eq "inputs" && ![dict exists $::__nl_pi $name]} continue
    set k 0
    foreach it [$net getITerms] { if {[$it getIoType] eq "INPUT"} { incr k } }
    lappend rows [list $name $k]
  }
  set rows [lsort -integer -decreasing -index 1 [lsort -dictionary -index 0 $rows]]
  __mcp_rec considered [llength $rows]
  set i 0
  set max [lindex $rows 0 1]
  foreach r $rows {
    if {$i >= $top_n && [lindex $r 1] != $max} break
    __mcp_rec fo {*}$r
    incr i
  }
}

proc __nl_rename {kind old new} {
  set block [__nl_block]
  if {$kind eq "instance"} {
    set obj [$block findInst $old]
    if {[__nl_null $obj]} { error "No gate named '$old'." }
    if {![__nl_null [$block findInst $new]]} { error "A gate named '$new' already exists." }
  } else {
    set obj [$block findNet $old]
    if {[__nl_null $obj]} { __nl_missing $old }
    if {[llength [$obj getBTerms]]} {
      error "'$old' is a port net; renaming it would change the module interface."
    }
    if {[dict exists $::__nl_const $old]} { error "'$old' is a constant net." }
    if {![__nl_null [$block findNet $new]] || ![__nl_null [$block findBTerm $new]]} {
      error "A net or port named '$new' already exists."
    }
  }
  set ok [$obj rename $new]
  if {$ok ne "" && !$ok} { error "OpenDB refused to rename '$old' to '$new'." }
  if {$kind eq "instance"} {
    foreach it [$obj getITerms] {
      __mcp_rec conn [[$it getMTerm] getName] [__nl_const_text [__nl_net_name [$it getNet]]]
    }
    set seen [llength [__nl_exact get_cells $new]]
    set gone [expr {[llength [__nl_exact get_cells $old]] == 0}]
  } else {
    foreach it [$obj getITerms] {
      __mcp_rec conn "[[$it getInst] getName]/[[$it getMTerm] getName]" [string tolower [$it getIoType]]
    }
    set seen [llength [__nl_exact get_nets $new]]
    set gone [expr {[llength [__nl_exact get_nets $old]] == 0}]
  }
  __mcp_rec renamed $kind $old $new [expr {$seen && $gone}]
}

# write_verilog, then put the constants back: OpenROAD writes them as an undeclared,
# undriven net (.SN(one_)), which drops the value.
proc __nl_write_verilog {path overwrite restore} {
  set p [file normalize $path]
  if {!$overwrite && [file exists $p]} {
    error "File already exists: $p (pass overwrite=true to replace it)"
  }
  if {![file isdirectory [file dirname $p]]} {
    error "Directory does not exist: [file dirname $p]"
  }
  write_verilog $p
  set counts {}
  if {$restore && [dict size $::__nl_const]} {
    set f [open $p r]
    set text [read $f]
    close $f
    dict for {name value} $::__nl_const {
      set lit [expr {$value ? "1'b1" : "1'b0"}]
      set n [regsub -all [format {\(\s*%s\s*\)} $name] $text "($lit)" text]
      regsub -all -line [format {^\s*wire\s+%s\s*;\s*$\n?} $name] $text "" text
      lappend counts $lit $n
    }
    set f [open $p w]
    fconfigure $f -translation lf
    puts -nonewline $f $text
    close $f
  }
  __mcp_rec written $p {*}$counts
}

proc __nl_summary {} {
  set block [__nl_block]
  set counts [dict create]
  foreach inst [$block getInsts] { dict incr counts [[$inst getMaster] getName] }
  dict for {m n} $counts { __mcp_rec count $m $n }
  dict for {m _} $::__nl_seq { __mcp_rec seq $m }
  __mcp_rec io [dict size $::__nl_pi] [dict size $::__nl_po] [llength [$block getNets]]
  dict for {name v} $::__nl_const {
    __mcp_rec cnet $name [__nl_const_text $name] [llength [[$block findNet $name] getITerms]]
  }
}
"""
