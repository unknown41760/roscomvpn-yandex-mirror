package main

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"
	"sort"
	"strings"

	"github.com/v2fly/v2ray-core/v5/app/router/routercommon"
	"google.golang.org/protobuf/proto"
)

type stringList []string

func (values *stringList) String() string {
	return strings.Join(*values, ",")
}

func (values *stringList) Set(value string) error {
	value = strings.TrimSpace(value)
	if value == "" {
		return errors.New("category must not be empty")
	}
	*values = append(*values, strings.ToUpper(value))
	return nil
}

func readProto(path string, message proto.Message) error {
	data, err := os.ReadFile(path)
	if err != nil {
		return fmt.Errorf("read %s: %w", path, err)
	}
	if err := proto.Unmarshal(data, message); err != nil {
		return fmt.Errorf("decode %s as V2Ray geodata: %w", path, err)
	}
	return nil
}

func geoIPCode(entry *routercommon.GeoIP) string {
	if code := strings.TrimSpace(entry.GetCountryCode()); code != "" {
		return strings.ToUpper(code)
	}
	return strings.ToUpper(strings.TrimSpace(entry.GetCode()))
}

func geoSiteCode(entry *routercommon.GeoSite) string {
	if code := strings.TrimSpace(entry.GetCountryCode()); code != "" {
		return strings.ToUpper(code)
	}
	return strings.ToUpper(strings.TrimSpace(entry.GetCode()))
}

func mergeGeoIP(args []string) error {
	flags := flag.NewFlagSet("merge-geoip", flag.ContinueOnError)
	basePath := flags.String("base", "", "RoscomVPN base geoip.dat")
	sourcePath := flags.String("source", "", "standard source geoip.dat")
	outputPath := flags.String("output", "", "output geoip.dat")
	category := flags.String("category", "RU", "category to add")
	if err := flags.Parse(args); err != nil {
		return err
	}
	if *basePath == "" || *sourcePath == "" || *outputPath == "" {
		return errors.New("--base, --source, and --output are required")
	}

	wanted := strings.ToUpper(strings.TrimSpace(*category))
	base := new(routercommon.GeoIPList)
	if err := readProto(*basePath, base); err != nil {
		return err
	}

	for _, entry := range base.GetEntry() {
		if geoIPCode(entry) == wanted {
			if len(entry.GetCidr()) == 0 {
				return fmt.Errorf("base category %s contains no CIDRs", wanted)
			}
			return writeProto(*outputPath, base)
		}
	}

	source := new(routercommon.GeoIPList)
	if err := readProto(*sourcePath, source); err != nil {
		return err
	}

	for _, entry := range source.GetEntry() {
		if geoIPCode(entry) != wanted {
			continue
		}
		if len(entry.GetCidr()) == 0 {
			return fmt.Errorf("source category %s contains no CIDRs", wanted)
		}
		base.Entry = append(base.Entry, proto.Clone(entry).(*routercommon.GeoIP))
		return writeProto(*outputPath, base)
	}

	return fmt.Errorf("source geoip.dat does not contain category %s", wanted)
}

func writeProto(path string, message proto.Message) error {
	data, err := (proto.MarshalOptions{Deterministic: true}).Marshal(message)
	if err != nil {
		return fmt.Errorf("encode %s: %w", path, err)
	}
	if err := os.WriteFile(path, data, 0o644); err != nil {
		return fmt.Errorf("write %s: %w", path, err)
	}
	return nil
}

type validationSummary struct {
	GeositeCategories map[string]int `json:"geosite_categories"`
	GeoIPCategories   map[string]int `json:"geoip_categories"`
}

func validate(args []string) error {
	flags := flag.NewFlagSet("validate", flag.ContinueOnError)
	geositePath := flags.String("geosite", "", "geosite.dat to validate")
	geoIPPath := flags.String("geoip", "", "geoip.dat to validate")
	var requiredGeosite stringList
	var requiredGeoIP stringList
	flags.Var(&requiredGeosite, "geosite-category", "required geosite category (repeatable)")
	flags.Var(&requiredGeoIP, "geoip-category", "required geoip category (repeatable)")
	if err := flags.Parse(args); err != nil {
		return err
	}
	if *geositePath == "" || *geoIPPath == "" {
		return errors.New("--geosite and --geoip are required")
	}

	geosite := new(routercommon.GeoSiteList)
	if err := readProto(*geositePath, geosite); err != nil {
		return err
	}
	geoIP := new(routercommon.GeoIPList)
	if err := readProto(*geoIPPath, geoIP); err != nil {
		return err
	}

	summary := validationSummary{
		GeositeCategories: make(map[string]int),
		GeoIPCategories:   make(map[string]int),
	}
	for _, entry := range geosite.GetEntry() {
		code := geoSiteCode(entry)
		if code != "" {
			summary.GeositeCategories[code] += len(entry.GetDomain())
		}
	}
	for _, entry := range geoIP.GetEntry() {
		code := geoIPCode(entry)
		if code != "" {
			summary.GeoIPCategories[code] += len(entry.GetCidr())
		}
	}

	missing := make([]string, 0)
	for _, code := range requiredGeosite {
		if summary.GeositeCategories[code] == 0 {
			missing = append(missing, "geosite:"+strings.ToLower(code))
		}
	}
	for _, code := range requiredGeoIP {
		if summary.GeoIPCategories[code] == 0 {
			missing = append(missing, "geoip:"+strings.ToLower(code))
		}
	}
	if len(missing) != 0 {
		sort.Strings(missing)
		return fmt.Errorf("required categories are missing or empty: %s", strings.Join(missing, ", "))
	}

	encoder := json.NewEncoder(os.Stdout)
	encoder.SetIndent("", "  ")
	if err := encoder.Encode(summary); err != nil {
		return fmt.Errorf("write validation summary: %w", err)
	}
	return nil
}

func usage() {
	fmt.Fprintln(os.Stderr, "usage: geodata-tool <merge-geoip|validate> [options]")
}

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}

	var err error
	switch os.Args[1] {
	case "merge-geoip":
		err = mergeGeoIP(os.Args[2:])
	case "validate":
		err = validate(os.Args[2:])
	default:
		usage()
		err = fmt.Errorf("unknown command %q", os.Args[1])
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "geodata-tool:", err)
		os.Exit(1)
	}
}
